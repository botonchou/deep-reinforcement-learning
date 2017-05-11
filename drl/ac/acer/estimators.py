import sys
import tensorflow as tf
from drl.ac.utils import *
from drl.ac.distributions import *
from drl.ac.models import *
from drl.ac.policies import build_policy
from drl.ac.estimators import *
from drl.ac.worker import Worker
from drl.ac.acer.worker import AcerWorker
from drl.optimizers import TrpoOptimizer

FLAGS = tf.flags.FLAGS
batch_size = FLAGS.batch_size
seq_length = FLAGS.seq_length

class AcerEstimator():
    def __init__(self, add_summaries=False, trainable=True, use_naive_policy=True):

        self.trainable = trainable

        self.avg_net = getattr(AcerEstimator, "average_net", self)

        scope_name = tf.get_variable_scope().name + '/'

        with tf.name_scope("inputs"):
            # TODO When seq_length is None, use seq_length + 1 is somewhat counter-intuitive.
            # Come up a solution to pass seq_length+1 and seq_length at the same time.
            # maybe a assertion ? But that could be hard to understand
            self.seq_length = tf.placeholder(tf.int32, [], "seq_length")
            self.state = get_state_placeholder()
            self.a = tf.placeholder(FLAGS.dtype, [seq_length, batch_size, FLAGS.num_actions], "actions")
            self.r = tf.placeholder(FLAGS.dtype, [seq_length, batch_size, 1], "rewards")
            self.done = tf.placeholder(tf.bool, [batch_size, 1], "done")

        with tf.variable_scope("shared"):
            shared, self.lstm = build_network(self.state, scope_name, add_summaries)

        # For k-step rollout s_i, i = 0, 1, ..., k-1, we need one additional
        # state s_k s.t. we can bootstrap value from it, i.e. we need V(s_k)
        with tf.variable_scope("V"):
            self.value_all = value = state_value_network(shared, self.state.steps)
            value *= tf.Variable(1, dtype=FLAGS.dtype, name="value_scale", trainable=FLAGS.train_value_scale)
            self.value_last = value[-1:, ...] * tf.cast(~self.done, FLAGS.dtype)[None, ...]
            self.value = value[:self.seq_length, ...]

        with tf.variable_scope("shared-policy"):
            if not FLAGS.share_network:
                # FIXME right now this only works for non-lstm version
                shared, lstm2 = build_network(self.state, scope_name, add_summaries)
                self.lstm.inputs.update(lstm2.inputs)
                self.lstm.outputs.update(lstm2.outputs)

            shared = shared[:self.seq_length, ...]

        self.state.update(self.lstm.inputs)

        with tf.variable_scope("policy"):
            self.pi, self.pi_behavior = build_policy(shared, FLAGS.policy_dist)

        with tf.name_scope("output"):
            self.a_prime = tf.squeeze(self.pi.sample_n(1), 0)
            self.action_and_stats = [self.a_prime, self.pi.stats]

        if not self.trainable:
            return

        with tf.variable_scope("A"):
            Q_tilt = stochastic_dueling_network(shared, self.value, self.pi)

        with tf.variable_scope("Q"):
            self.Q_tilt_a = Q_tilt(self.a, name="Q_tilt_a")
            self.Q_tilt_a_prime = Q_tilt(self.a_prime, name="Q_tilt_a_prime")

            # Compute the importance sampling weight \rho and \rho^{'}
            with tf.name_scope("rho"):
                self.rho = compute_rho(self.a, self.pi, self.pi_behavior)
                self.rho = tf_print(self.rho)
                self.rho_prime = compute_rho(self.a_prime, self.pi, self.pi_behavior)
                self.rho_prime = tf_print(self.rho_prime)

            with tf.name_scope("c_i"):
                self.c = tf.minimum(tf_const(1.), self.rho ** (1. / FLAGS.num_actions), "c_i")
                tf.logging.info("c.shape = {}".format(tf_shape(self.c)))

            with tf.name_scope("Q_Retrace"):
                self.Q_ret, self.Q_opc = compute_Q_ret_Q_opc(
                    self.value, self.value_last, self.c, self.r, self.Q_tilt_a
                )

        with tf.name_scope("losses"):
            # Surrogate loss is the loss tensor we passed to optimizer for
            # automatic gradient computation, it uses lots of stop_gradient.
            # Therefore it's different from the true loss (self.loss)
            self.pi_loss, self.pi_loss_sur = self.get_policy_loss(
                self.rho, self.pi, self.a, self.Q_opc, self.value,
                self.rho_prime, self.Q_tilt_a_prime, self.a_prime
            )

            self.vf_loss, self.vf_loss_sur = self.get_value_loss(
                self.Q_ret, self.Q_tilt_a, self.rho, self.value
            )

            self.entropy, self.entropy_loss = exploration_loss(self.pi)

            for loss in [self.pi_loss_sur, self.vf_loss_sur, self.entropy_loss]:
                assert len(loss.get_shape()) == 0

            self.loss_sur = (
                self.pi_loss_sur
                + self.vf_loss_sur * FLAGS.lr_vp_ratio
                + self.entropy_loss
            )

            self.loss = self.pi_loss + self.vf_loss + self.entropy_loss

            # Add regularization (L1 and L2)
            reg_vars = get_regularizable_vars()
            self.loss += FLAGS.l1_reg * l1_loss(reg_vars)
            self.loss += FLAGS.l2_reg * l2_loss(reg_vars)

        with tf.name_scope("grads_and_optimizer"):

            update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(update_ops):

                self.lr = tf.train.exponential_decay(
                    tf_const(FLAGS.learning_rate), FLAGS.global_step,
                    FLAGS.decay_steps, FLAGS.decay_rate, staircase=FLAGS.staircase
                )

                self.optimizer = tf.train.AdamOptimizer(self.lr)
                # self.optimizer = TrpoOptimizer(self.lr)

                self.grads_and_vars, self.global_norm = \
                    compute_gradients_with_checks(self.optimizer, self.loss_sur)

            # Collect all trainable variables initialized here
            self.var_list = [v for g, v in self.grads_and_vars]

        self.summaries = self.summarize(add_summaries)

    def get_initial_hidden_states(self, batch_size):
        return get_lstm_initial_states(self.lstm.inputs, batch_size)

    def predict(self, tensors, feed_dict, sess=None):
        sess = sess or tf.get_default_session()

        output, hidden_states = sess.run([
            tensors, self.lstm.outputs
        ], feed_dict)

        return output, hidden_states

    def update(self, tensors, feed_dict, sess=None):
        sess = sess or tf.get_default_session()

        with Worker.lock:
            output = sess.run(tensors, feed_dict)

        return output

    def predict_actions(self, state, sess=None):

        feed_dict = to_feed_dict(self, state)
        feed_dict[self.seq_length] = 1

        (a_prime, stats), hidden_states = self.predict(self.action_and_stats, feed_dict, sess)

        a_prime = a_prime[0, ...].T

        return a_prime, stats, hidden_states

    def get_policy_loss(self, rho, pi, a, Q_opc, value, rho_prime,
                        Q_tilt_a_prime, a_prime):

        tf.logging.info("Computing policy loss ...")

        with tf.name_scope("ACER"):
            pi_obj = self.compute_ACER_policy_obj(
                rho, pi, a, Q_opc, value, rho_prime, Q_tilt_a_prime, a_prime)

        pi_obj_sur, self.mean_KL = add_fast_TRPO_regularization(
            pi, self.avg_net.pi, pi_obj)
        """
        pi_obj_sur = pi_obj
        """

        # loss is the negative of objective function
        loss, loss_sur = -pi_obj, -pi_obj_sur

        return reduce_seq_batch_dim(loss, loss_sur)

    def get_value_loss(self, Q_ret, Q_tilt_a, rho, value):

        tf.logging.info("Computing value loss ...")

        Q_diff = tf.stop_gradient(Q_ret - Q_tilt_a)

        # L2 norm as loss function
        Q_l2_loss = 0.5 * tf.square(Q_diff)

        # surrogate loss function for L2-norm of Q and V, the derivatives of
        # (-Q_diff * Q_tilt_a) is the same as that of (0.5 * tf.square(Q_diff))
        Q_l2_loss_sur = -Q_diff * Q_tilt_a
        V_l2_loss_sur = -Q_diff * value * tf.minimum(tf_const(1.), rho)

        # Compute the objective function (obj) we try to maximize
        loss     = Q_l2_loss
        loss_sur = Q_l2_loss_sur + V_l2_loss_sur

        return reduce_seq_batch_dim(loss, loss_sur)

    def compute_ACER_policy_obj(self, rho, pi, a, Q_opc, value, rho_prime,
                                 Q_tilt_a_prime, a_prime):

        # compute gradient with importance weight truncation using c = 10
        c = tf_const(FLAGS.importance_weight_truncation_threshold)

        with tf.name_scope("truncation"):
            with tf.name_scope("truncated_importance_weight"):
                self.rho_bar = rho_bar = tf.minimum(c, rho)

            with tf.name_scope("d_log_prob_a"):
                a = tf_print(a)
                self.log_a = log_a = pi.log_prob(a)[..., None]
                log_a = tf_print(log_a)

            with tf.name_scope("target_1"):
                self.target_1 = target_1 = self.Q_opc - self.value
                target_1 = tf_print(target_1)

            # Policy gradient should only flow backs from log \pi
            truncation = tf.stop_gradient(rho_bar * target_1) * log_a
            truncation = tf_print(truncation)

        # compute bias correction term
        with tf.name_scope("bias_correction"):
            with tf.name_scope("bracket_plus"):
                self.plus = plus = tf.nn.relu(1. - c / rho_prime)
                plus = tf_print(plus)

            with tf.name_scope("d_log_prob_a_prime"):
                a_prime = tf_print(a_prime)
                self.log_ap = log_ap = pi.log_prob(a_prime)[..., None]
                log_ap = tf_print(log_ap)

            with tf.name_scope("target_2"):
                self.target_2 = target_2 = Q_tilt_a_prime - value
                target_2 = tf_print(target_2)

            # Policy gradient should only flow backs from log \pi
            bias_correction = tf.stop_gradient(plus * target_2) * log_ap
            bias_correction = tf_print(bias_correction)

        # g is called "truncation with bias correction" in ACER
        obj = truncation + bias_correction
        obj = tf_print(obj)

        return obj

    def summarize(self, add_summaries):

        if not add_summaries:
            return tf.no_op()

        # sum over rewards along the sequence dimension to get total return
        # and take mean along the batch dimension
        self.total_return = tf.reduce_mean(tf.reduce_sum(self.r, axis=0))

        keys_to_summarize = [
            "vf_loss", "pi_loss", "entropy", "loss",
            "total_return", "seq_length"
        ]

        tf.logging.info("Adding summaries ...")
        with tf.name_scope("summaries"):
            for key in keys_to_summarize:
                tf.summary.scalar(key, getattr(self, key))

        return tf.summary.merge_all()

    @staticmethod
    def create_averge_network():
        if "average_net" not in AcerEstimator.__dict__:
            with tf.variable_scope("average_net"):
                AcerEstimator.average_net = AcerEstimator(add_summaries=False)

AcerEstimator.Worker = AcerWorker
