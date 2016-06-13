import numpy as np
import os
import yaml
import cPickle as pickle
import lasagne
import theano.tensor as T
import nolearn.lasagne
from ml_helpers import minibatch_generators as mbg
import matplotlib.pyplot as plt


def force_make_dir(dirpath):
    if not os.path.exists(dirpath):
        os.makedirs(dirpath)
    return dirpath


class Log1Plus(lasagne.layers.Layer):
    def __init__(self, incoming, off=lasagne.init.Constant(1.0), mult=lasagne.init.Constant(1.0), **kwargs):
        super(Log1Plus, self).__init__(incoming, **kwargs)
        num_channels = self.input_shape[1]
        self.off = self.add_param(off, shape=(num_channels,), name='off')
        self.mult = self.add_param(mult, shape=(num_channels,), name='mult')

    def get_output_for(self, input, **kwargs):
        return T.log(self.off.dimshuffle('x', 0, 'x', 'x') + self.mult.dimshuffle('x', 0, 'x', 'x') * input)

    def get_output_shape_for(self, input_shape):
        return input_shape


class SpecSampler(object):

    def __init__(self, batch_size, hww, do_aug, learn_log, randomise=False,
            seed=None, balanced=True):
        self.do_aug = do_aug
        self.learn_log = learn_log
        self.hww = hww
        self.seed = seed
        self.randomise = randomise
        self.balanced = balanced
        self.batch_size = batch_size

    def __call__(self, X, y=None):
        blank_spec = np.zeros((X[0].shape[0], 2*self.hww))
        self.specs = np.hstack([blank_spec] + X + [blank_spec])[None, ...]

        blank_label = np.zeros(2*self.hww) - 1
        if y is not None:
            labels = [yy > 0 for yy in y]
        else:
            labels = [np.zeros(self.specs.shape[2] - 4*self.hww)]

        self.labels = np.hstack([blank_label] + labels + [blank_label])

        which_spec = [ii * np.ones(xx.shape[1]) for ii, xx in enumerate(X)]
        self.which_spec = np.hstack([blank_label] + which_spec + [blank_label]).astype(np.int32)

        if self.learn_log:
            self.medians = np.zeros((len(X), X[0].shape[0], self.hww*2))
            for idx, spec in enumerate(X):
                self.medians[idx] = np.median(spec, axis=1, keepdims=True)

        assert self.labels.shape[0] == self.specs.shape[2]
        return self

    def __iter__(self): ##, num_per_class, seed=None
        #num_samples = num_per_class * 2
        channels = self.specs.shape[0]
        height = self.specs.shape[1]

        if self.seed is not None:
            np.random.seed(self.seed)

        idxs = np.where(self.labels >= 0)[0]
        for sampled_locs, y in mbg.minibatch_iterator(idxs, self.labels[idxs],
            self.batch_size, randomise=self.randomise, balanced=self.balanced,
                class_size='smallest'):

            # extract the specs
            bs = y.shape[0]  # avoid using self.batch_size as last batch may be smaller
            X = np.zeros((bs, channels, height, self.hww*2), np.float32)
            y = np.zeros(bs) * np.nan
            if self.learn_log:
                X_medians = np.zeros((bs, channels, height, self.hww*2), np.float32)
            count = 0

            for loc in sampled_locs:
                try:
                    X[count] = self.specs[:, :, (loc-self.hww):(loc+self.hww)]
                except:
                    import pdb; pdb.set_trace()
                y[count] = self.labels[loc]
                if self.learn_log:
                    which = self.which_spec[loc]
                    X_medians[count] = self.medians[which]
                count += 1

            # doing augmentation
            if self.do_aug:
                if self.learn_log:
                    mult = (1.0 + np.random.randn(bs, 1, 1, 1) * 0.1)
                    mult = np.clip(mult, 0.1, 200)
                    X *= mult
                else:
                    X *= (1.0 + np.random.randn(bs, 1, 1, 1) * 0.1)
                    X += np.random.randn(bs, 1, 1, 1) * 0.1
                    if np.random.rand() > 0.9:
                        X += np.roll(X, 1, axis=0) * np.random.randn()

            if self.learn_log:
                xb = {'input': X.astype(np.float32), 'input_med': X_medians.astype(np.float32)}
                yield xb, y.astype(np.int32)

            else:
                yield X.astype(np.float32), y.astype(np.int32)

#
# class MyBatch(nolearn.lasagne.BatchIterator):
#     def __iter__(self):
#         for _ in range(32):
#             yield self.X.sample(self.batch_size)
#
#
# class MyBatchTest(nolearn.lasagne.BatchIterator):
#     def __iter__(self):
#         for idx in range(128):
#             yield self.X.sample(self.batch_size, seed=idx)


class HelpersBaseClass(object):
    # sub dir is a class attribute so subclasses can override it
    subdir = ""

    def __init__(self, logging_dir):
        self.savedir = force_make_dir(logging_dir + self.subdir)


class SaveHistory(HelpersBaseClass):
    def __init__(self, logging_dir):
        super(SaveHistory, self).__init__(logging_dir)
        self.savepath = self.savedir + "history.yaml"
        with open(self.savepath, 'w'):
            pass

    def __call__(self, net, history):
        '''
        Dumps nolearn history to disk, one line at a time
        '''
        # handling numpy bool values
        for key, item in history[-1].iteritems():
            if type(item) == np.bool_:
                history[-1][key] = bool(item)

        yaml.dump([history[-1]], open(self.savepath, 'a'))


class SaveWeights(HelpersBaseClass):
    subdir = "/weights/"

    def __init__(self, logging_dir, save_weights_every, new_file_every):
        super(SaveWeights, self).__init__(logging_dir)
        self.save_weights_every = save_weights_every
        self.new_file_every = new_file_every

    def __call__(self, net, history):
        '''
        Dumps weights to disk. Saves weights every epoch, but only starts a
        new file every X epochs.
        '''
        if (len(history) - 1) % self.save_weights_every == 0:
            filenum = (len(history) - 1) / self.new_file_every
            savepath = self.savedir + "weights_%06d.pkl" % filenum
            net.save_params_to(savepath)


class EarlyStopping(object):
    # https://github.com/dnouri/nolearn/issues/18
    def __init__(self, patience=100):
        self.patience = patience
        self.best_valid = np.inf
        self.best_valid_epoch = 0
        self.best_weights = None

    def __call__(self, nn, train_history):
        current_valid = train_history[-1]['valid_loss']
        current_epoch = train_history[-1]['epoch']
        if current_valid < self.best_valid:
            self.best_valid = current_valid
            self.best_valid_epoch = current_epoch
            self.best_weights = nn.get_all_params_values()  # updated
        elif self.best_valid_epoch + self.patience < current_epoch:
            if nn.verbose:
                print("Early stopping.")
                print("Best valid loss was {:.6f} at epoch {}.".format(
                    self.best_valid, self.best_valid_epoch))
            nn.load_params_from(self.best_weights)
            if nn.verbose:
                print("Weights set.")
            raise StopIteration()
