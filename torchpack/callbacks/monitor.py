import json
import operator
import os
import re
import shutil
import time
from collections import defaultdict
from datetime import datetime

import numpy as np
import six
import tensorflow as tf

from .base import Callback
from ..utils import logger

__all__ = ['Monitor', 'Monitors', 'TFEventWriter', 'JSONWriter', 'ScalarPrinter']


def image_to_nhwc(arr):
    if arr.ndim == 4:
        pass
    elif arr.ndim == 3:
        if arr.shape[-1] in [1, 3, 4]:
            arr = arr[np.newaxis, :]
        else:
            arr = arr[:, :, :, np.newaxis]
    elif arr.ndim == 2:
        arr = arr[np.newaxis, :, :, np.newaxis]
    else:
        raise ValueError("Array of shape {} is not an image!".format(arr.shape))
    return arr


class Monitor(Callback):
    """
    Base class for monitors which monitor a training progress, by processing different types of
    summary/statistics from trainer.
    """

    def process_summary(self, summary):
        """
        Process a tf.Summary.
        """
        pass

    def process(self, name, val):
        """
        Process a key-value pair.
        """
        pass

    def process_scalar(self, name, val):
        """
        Args:
            val: a scalar
        """
        pass

    def process_image(self, name, val):
        """
        Args:
            val (np.ndarray): 4D (NHWC) numpy array of images in range [0,255].
                If channel is 3, assumed to be RGB.
        """
        pass

    def process_event(self, evt):
        """
        Args:
            evt (tf.Event): the most basic format acceptable by tensorboard.
                It could include Summary, RunMetadata, LogMessage, and more.
        """
        pass


class NoneMonitor(Monitor):
    def __init__(self, name=None):
        super(NoneMonitor, self).__init__()
        self._name = name

    def __str__(self):
        if self._name is not None:
            return "NoneMonitor({})".format(self._name)
        else:
            return 'NoneMonitor'


class Monitors(Callback):
    """
    Merge monitors together for trainer to use.
    In training, each trainer will create a :class:`Monitors` instance,
    and you can access it through ``trainer.monitors``.
    You should use ``trainer.monitors`` for logging and it will dispatch your
    logs to each sub-monitor.
    """

    def __init__(self, monitors):
        super(Monitors, self).__init__()
        self._scalar_history = ScalarHistory()
        self._monitors = monitors + [self._scalar_history]
        for monitor in self._monitors:
            assert isinstance(monitor, Monitor), monitor

    def _setup_graph(self):
        # scalar_history's other methods were not called.
        # but they are not useful for now
        self._scalar_history.setup_graph(self.trainer)

    def _dispatch(self, func):
        for m in self._monitors:
            func(m)

    def put_summary(self, summary):
        """
        Put a `tf.Summary`.
        """
        if isinstance(summary, six.binary_type):
            summary = tf.Summary.FromString(summary)
        assert isinstance(summary, tf.Summary), type(summary)

        # TODO other types
        for val in summary.value:
            if val.WhichOneof('value') == 'simple_value':
                val.tag = re.sub('tower[0-9]+/', '', val.tag)  # TODO move to subclasses

                # TODO This hack is still needed, seem to disappear only when
                # compiled from source.
                suffix = '-summary'  # tensorflow#6150, tensorboard#59
                if val.tag.endswith(suffix):
                    val.tag = val.tag[:-len(suffix)]

                self._dispatch(lambda m: m.process_scalar(val.tag, val.simple_value))

        for monitor in self._monitors:
            monitor.process_summary(summary)

    def put_scalar(self, name, val):
        """
        Put a scalar.
        """
        if isinstance(val, np.integer):
            val = int(val)
        if isinstance(val, np.floating):
            val = float(val)

        for monitor in self._monitors:
            monitor.process_scalar(name, val)

    def put_image(self, name, val):
        """
        Put an image.
        Args:
            name (str):
            val (np.ndarray): 2D, 3D (HWC) or 4D (NHWC) numpy array of images
                in range [0,255]. If channel is 3, assumed to be RGB.
        """
        assert isinstance(val, np.ndarray)
        arr = image_to_nhwc(val)

        for monitor in self._monitors:
            monitor.process_image(name, arr)

    def put_event(self, event):
        """
        Put an :class:`tf.Event`.
        `step` and `wall_time` fields of :class:`tf.Event` will be filled automatically.
        Args:
            event (tf.Event):
        """
        event.step = self.global_step
        event.wall_time = time.time()

        for monitor in self._monitors:
            monitor.process_event(event)

    def get_latest(self, name):
        """
        Get latest scalar value of some data.
        If you run multiprocess training, keep in mind that
        the data is perhaps only available on chief process.
        Returns:
            scalar
        """
        return self._scalar_history.get_latest(name)[1]

    def get_history(self, name):
        """
        Get a history of the scalar value of some data.
        If you run multiprocess training, keep in mind that
        the data is perhaps only available on chief process.
        Returns:
            a list of (global_step, value) pairs: history data for this scalar
        """
        return self._scalar_history.get_history(name)


class TFEventWriter(Monitor):
    """
    Write summaries to TensorFlow event file.
    """

    def __init__(self, logdir=None, max_queue=10, flush_secs=120, split_files=False):
        """
        Args:
            logdir: ``logger.get_logger_dir()`` by default.
            max_queue, flush_secs: Same as in :class:`tf.summary.FileWriter`.
            split_files: if True, split events to multiple files rather than
                append to a single file. Useful on certain filesystems where append is expensive.
        """
        super(TFEventWriter, self).__init__()

        if logdir is None:
            logdir = logger.get_logger_dir()
        assert tf.gfile.IsDirectory(logdir), logdir
        self._logdir = logdir
        self._max_queue = max_queue
        self._flush_secs = flush_secs
        self._split_files = split_files

    def __new__(cls, logdir=None, max_queue=10, flush_secs=120, **kwargs):
        if logdir is None:
            logdir = logger.get_logger_dir()

        if logdir is not None:
            return super(TFEventWriter, cls).__new__(cls)
        else:
            logger.warn("logger directory was not set. Ignore TFEventWriter.")
            return NoneMonitor("TFEventWriter")

    def _setup_graph(self):
        self._writer = tf.summary.FileWriter(
            self._logdir, graph=tf.get_default_graph(),
            max_queue=self._max_queue, flush_secs=self._flush_secs
        )

    def process_scalar(self, name, val):
        self._writer.add_scalar(name, val, self.global_step)

    def process_summary(self, summary):
        self._writer.add_summary(summary, self.global_step)

    def process_event(self, event):
        self._writer.add_event(event)

    def _trigger(self):  # flush every epoch
        self._writer.flush()
        if self._split_files:
            self._writer.close()
            self._writer.reopen()  # open new file

    def _after_train(self):
        self._writer.close()


class JSONWriter(Monitor):
    """
    Write all scalar data to a json file under ``logger.get_logger_dir()``, grouped by their global step.
    If found an earlier json history file, will append to it.
    """

    FILENAME = 'stats.json'
    """
    The name of the json file. Do not change it.
    """

    def __new__(cls):
        if logger.get_logger_dir():
            return super(JSONWriter, cls).__new__(cls)
        else:
            logger.warn("logger directory was not set. Ignore JSONWriter.")
            return NoneMonitor("JSONWriter")

    @staticmethod
    def load_existing_json():
        """
        Look for an existing json under :meth:`logger.get_logger_dir()` named "stats.json",
        and return the loaded list of statistics if found. Returns None otherwise.
        """
        dir = logger.get_logger_dir()
        fname = os.path.join(dir, JSONWriter.FILENAME)
        if tf.gfile.Exists(fname):
            with open(fname) as f:
                stats = json.load(f)
                assert isinstance(stats, list), type(stats)
                return stats
        return None

    @staticmethod
    def load_existing_epoch_number():
        """
        Try to load the latest epoch number from an existing json stats file (if any).
        Returns None if not found.
        """
        stats = JSONWriter.load_existing_json()
        try:
            return int(stats[-1]['epoch_num'])
        except Exception:
            return None

    # initialize the stats here, because before_train from other callbacks may use it
    def _setup_graph(self):
        self._stats = []
        self._stat_now = {}
        self._last_gs = -1

    def _before_train(self):
        stats = JSONWriter.load_existing_json()
        self._fname = os.path.join(logger.get_logger_dir(), JSONWriter.FILENAME)
        if stats is not None:
            try:
                epoch = stats[-1]['epoch_num'] + 1
            except Exception:
                epoch = None

            # check against the current training settings
            # therefore this logic needs to be in before_train stage
            starting_epoch = self.trainer.loop.starting_epoch
            if epoch is None or epoch == starting_epoch:
                logger.info("Found existing JSON inside {}, will append to it.".format(logger.get_logger_dir()))
                self._stats = stats
            else:
                logger.warn(
                    "History epoch={} from JSON is not the predecessor of the current starting_epoch={}".format(
                        epoch - 1, starting_epoch))
                logger.warn("If you want to resume old training, either use `AutoResumeTrainConfig` "
                            "or correctly set the new starting_epoch yourself to avoid inconsistency. ")

                backup_fname = JSONWriter.FILENAME + '.' + datetime.now().strftime('%m%d-%H%M%S')
                backup_fname = os.path.join(logger.get_logger_dir(), backup_fname)

                logger.warn("Now, we will train with starting_epoch={} and backup old json to {}".format(
                    self.trainer.loop.starting_epoch, backup_fname))
                shutil.move(self._fname, backup_fname)

        # in case we have something to log here.
        self._trigger()

    def _trigger_step(self):
        # will do this in trigger_epoch
        if self.local_step != self.trainer.steps_per_epoch - 1:
            self._trigger()

    def _trigger_epoch(self):
        self._trigger()

    def process_scalar(self, name, val):
        self._stat_now[name] = val

    def _trigger(self):
        """
        Add stats to json and dump to disk.
        Note that this method is idempotent.
        """
        if len(self._stat_now):
            self._stat_now['epoch_num'] = self.epoch_num
            self._stat_now['global_step'] = self.global_step

            self._stats.append(self._stat_now)
            self._stat_now = {}
            self._write_stat()

    def _write_stat(self):
        tmp_filename = self._fname + '.tmp'
        try:
            with open(tmp_filename, 'w') as f:
                json.dump(self._stats, f)
            shutil.move(tmp_filename, self._fname)
        except IOError:  # disk error sometimes..
            logger.exception("Exception in JSONWriter._write_stat()!")


class ScalarPrinter(Monitor):
    """
    Print scalar data into terminal.
    """

    def __init__(self, enable_step=False, enable_epoch=True, whitelist=None, blacklist=None):
        """
        Args:
            enable_step, enable_epoch (bool): whether to print the
                monitor data (if any) between steps or between epochs.
            whitelist (list[str] or None): A list of regex. Only names
                matching some regex will be allowed for printing.
                Defaults to match all names.
            blacklist (list[str] or None): A list of regex. Names matching
                any regex will not be printed. Defaults to match no names.
        """
        super(ScalarPrinter, self).__init__()

        def compile_regex(rs):
            if rs is None:
                return None
            rs = set([re.compile(r) for r in rs])
            return rs

        self._whitelist = compile_regex(whitelist)
        if blacklist is None:
            blacklist = []
        self._blacklist = compile_regex(blacklist)

        self._enable_step = enable_step
        self._enable_epoch = enable_epoch
        self._dict = {}

    # in case we have something to log here.
    def _before_train(self):
        self._trigger()

    def _trigger_step(self):
        if self._enable_step:
            if self.local_step != self.trainer.steps_per_epoch - 1:
                # not the last step
                self._trigger()
            else:
                if not self._enable_epoch:
                    self._trigger()
                # otherwise, will print them together

    def _trigger_epoch(self):
        if self._enable_epoch:
            self._trigger()

    def process_scalar(self, name, val):
        self._dict[name] = float(val)

    def _trigger(self):
        def match_regex_list(regexs, name):
            for regex in regexs:
                if regex.search(name) is not None:
                    return True
            return False

        for k, v in sorted(self._dict.items(), key=operator.itemgetter(0)):
            if self._whitelist is None or match_regex_list(self._whitelist, k):
                if not match_regex_list(self._blacklist, k):
                    logger.info('{}: {:.5g}'.format(k, v))

        self._dict = {}


class ScalarHistory(Monitor):
    """
    Only internally used by monitors.
    """

    def __init__(self):
        super(ScalarHistory, self).__init__()
        self._dict = defaultdict(list)

    def process_scalar(self, name, val):
        self._dict[name].append((self.global_step, float(val)))

    def get_latest(self, name):
        hist = self._dict[name]
        if len(hist) == 0:
            raise KeyError('No available data for the key: {}'.format(name))
        return hist[-1]

    def get_history(self, name):
        return self._dict[name]
