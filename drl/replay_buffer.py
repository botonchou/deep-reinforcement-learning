import zlib
import cPickle
import itertools
import tensorflow as tf
from attrdict import AttrDict
from collections import deque
from drl.ac.utils import Timer, show_mem_usage
FLAGS = tf.flags.FLAGS

class ReplayBuffer(object):

    def __init__(self, fn=None, maxlen=None, compress=True, save_path=None):

        if fn:
            self.load(fn)
        else:
            self.maxlen = maxlen
            self.deque = deque(maxlen=maxlen)

        # keep last 1000 compress, decompress time for profiling purpose
        self.timer = AttrDict(
            compress = Timer("compress"),
            decompress = Timer("decompress")
        )

        self.compress = compress
        self.save_path = save_path

        # A thread-safe get-and-increment counter
        self.counter = itertools.count()

        # fixed items will not be pushed away by append method
        self.fixed_items = []

    def load(self, fn, fixed=False):
        tf.logging.info("Loading replay from {}".format(fn))
        data = cPickle.load(open(fn, 'rb'))
        for i in range(99):
            data.append(data[0])
        tf.logging.info("{} experiences loaded".format(len(data)))

        if fixed:
            self.fixed_items = list(data)
        else:
            self.deque = data
            self.maxlen = self.deque.maxlen

    def dump(self, fn):
        cPickle.dump(self.deque, open(fn, 'wb'), protocol=cPickle.HIGHEST_PROTOCOL)

    def _compress(self, item):
        self.timer.compress.tic()
        item = zlib.compress(cPickle.dumps(item, protocol=cPickle.HIGHEST_PROTOCOL))
        self.timer.compress.toc()
        return item

    def _decompress(self, item):
        self.timer.decompress.tic()
        item = cPickle.loads(zlib.decompress(item))
        self.timer.decompress.toc()
        return item

    def append(self, item):

        # itertools.count() starts from 0, so we need to skip i == 0
        i = self.counter.next()
        if i > 0 and i % self.maxlen == 0:
            show_mem_usage(self, "replay buffer")

            # auto save only when save_path is not None
            if self.save_path is not None:
                self.dump('{}/{:06d}.pkl'.format(self.save_path, i))

        if self.compress:
            item = self._compress(item)

        self.deque.append(item)

    def __len__(self):
        return len(self.deque) + len(self.fixed_items)

    def get(self, key):
        return self.deque[key]

    def __getitem__(self, key):
        n_fixed = len(self.fixed_items)
        if key < n_fixed:
            item = self.fixed_items[key]
        else:
            item = self.deque[key - n_fixed]

        if self.compress:
            item = self._decompress(item)

        return item
