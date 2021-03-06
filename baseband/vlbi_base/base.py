import io
import warnings
import numpy as np
from collections import namedtuple
import astropy.units as u
from astropy.utils import lazyproperty, deprecated


__all__ = ['VLBIStreamBase', 'VLBIStreamReaderBase', 'VLBIStreamWriterBase',
           'make_opener']


class VLBIFileBase(object):
    """VLBI file wrapper, used to add frame methods to a binary data file.

    The underlying file is stored in ``fh`` and all attributes that do not
    exist on the class itself are looked up on it.
    """

    def __init__(self, fh_raw):
        self.fh_raw = fh_raw

    def __getattr__(self, attr):
        """Try to get things on the current open file if it is not on self."""
        if not attr.startswith('_'):
            try:
                return getattr(self.fh_raw, attr)
            except AttributeError:
                pass
        #  __getattribute__ to raise appropriate error.
        return self.__getattribute__(attr)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self.fh_raw.close()

    def __repr__(self):
        return "{0}(fh_raw={1})".format(self.__class__.__name__, self.fh_raw)


class VLBIStreamBase(VLBIFileBase):
    """VLBI file wrapper, allowing access as a stream of data."""

    _frame_class = None
    _squeezed_shape = None

    def __init__(self, fh_raw, header0, sample_shape, bps, complex_data,
                 thread_ids, samples_per_frame, sample_rate, squeeze=True):
        super(VLBIStreamBase, self).__init__(fh_raw)
        self._header0 = header0
        self._sample_shape = sample_shape
        self._bps = bps
        self._complex_data = complex_data
        self.thread_ids = thread_ids
        self.samples_per_frame = samples_per_frame
        self.sample_rate = sample_rate
        self.offset = 0
        self.squeeze = squeeze

    @property
    def squeeze(self):
        """Whether data arrays have arrays with length unity removed.

        If `True`, such dimensions are removed in reading, and added back in
        writing."""
        return self._squeeze

    @squeeze.setter
    def squeeze(self, squeeze):
        self._squeeze = bool(squeeze)

    @property
    def sample_shape(self):
        """Shape of a data sample (possibly squeezed)."""
        if self.squeeze:
            if not self._squeezed_shape:
                field_names = getattr(self._sample_shape, '_fields', None)
                sqz_dims = [dim for dim in self._sample_shape if dim > 1]
                if field_names is None:
                    self._squeezed_shape = tuple(sqz_dims)
                else:
                    sqz_names = [field for field, dim in
                                 zip(field_names, self._sample_shape) if
                                 dim > 1]
                    sqz_shp_cls = namedtuple('SampleShape',
                                             ','.join(sqz_names))
                    self._squeezed_shape = sqz_shp_cls(*sqz_dims)
            return self._squeezed_shape
        else:
            return self._sample_shape

    def _unsqueeze(self, data):
        new_shape = list(data.shape)
        for i, dim in enumerate(self._sample_shape):
            if dim == 1:
                new_shape.insert(i + 1, 1)
        return data.reshape(new_shape)

    def _get_time(self, header):
        """Get time from a header."""
        # Subclasses can override this if information is needed beyond that
        # provided in the header.
        return header.time

    @lazyproperty
    def start_time(self):
        """Start time of the file.

        See also `time` for the time of the sample pointer's current offset,
        and (if available) `stop_time` for the time at the end of the file.
        """
        return self._get_time(self.header0)

    @deprecated('0.X', name='time0', alternative='start_time',
                obj_type='attribute')
    def get_time0(self):
        return self.start_time

    time0 = property(get_time0, None, None)

    @property
    def time(self):
        """Time of the sample pointer's current offset in file.

        See also `start_time` for the start time, and (if available)
        `stop_time` for the end time, of the file.
        """
        return self.tell(unit='time')

    @property
    def header0(self):
        """First header of the file."""
        return self._header0

    @property
    def bps(self):
        """Number of bits for each part (real or imaginary) of a sample."""
        return self._bps

    @property
    def complex_data(self):
        """Whether the decoded data is complex."""
        return self._complex_data

    @property
    def thread_ids(self):
        """Specific threads/channels to read."""
        return self._thread_ids

    @thread_ids.setter
    def thread_ids(self, ids):
        self._thread_ids = ids

    @property
    def samples_per_frame(self):
        """Number of complete samples per frame."""
        return self._samples_per_frame

    @samples_per_frame.setter
    def samples_per_frame(self, samples_per_frame):
        try:
            self._samples_per_frame = samples_per_frame.__index__()
        except Exception:
            raise TypeError("samples per frame must have an integer value.")

    @property
    def sample_rate(self):
        """Number of complete samples per second."""
        return self._sample_rate

    @sample_rate.setter
    def sample_rate(self, sample_rate):
        # Check if sample_rate is a time rate.
        try:
            sample_rate.to(u.Hz)
        except u.UnitsError as exc:
            exc.args += ("sample rate must have units of 1 / time.",)
            raise
        self._sample_rate = sample_rate

    def tell(self, unit=None):
        """Current offset in file.

        Parameters
        ----------
        unit : `~astropy.units.Unit` or str, optional
            Time unit the offset should be returned in.  By default, no unit
            is used, i.e., an integer enumerating samples is returned. For the
            special string 'time', the absolute time is calculated.

        Returns
        -------
        offset : int, `~astropy.units.Quantity`, or `~astropy.time.Time`
             Offset in current file (or time at current position)
        """
        if unit is None:
            return self.offset

        if unit == 'time':
            return self.start_time + self.tell(unit=u.s)

        return (self.offset / self.sample_rate).to(unit)

    def _frame_info(self):
        offset = (self.offset +
                  self.header0['frame_nr'] * self.samples_per_frame)
        framerate = int(np.round(
            (self.sample_rate / self.samples_per_frame).to_value(u.Hz)))
        full_frame_nr, extra = divmod(offset, self.samples_per_frame)
        dt, frame_nr = divmod(full_frame_nr, framerate)
        return dt, frame_nr, extra

    def __repr__(self):
        return ("<{s.__class__.__name__} name={s.name} offset={s.offset}\n"
                "    sample_rate={s.sample_rate},"
                " samples_per_frame={s.samples_per_frame},\n"
                "    sample_shape={s.sample_shape}, bps={s.bps},\n"
                "    {t}start_time={s.start_time.isot}>"
                .format(s=self, t=('thread_ids={0}, '.format(self.thread_ids)
                                   if self.thread_ids else '')))


class VLBIStreamReaderBase(VLBIStreamBase):

    def __init__(self, fh_raw, header0, sample_shape, bps, complex_data,
                 thread_ids, samples_per_frame, sample_rate=None,
                 squeeze=True):

        if sample_rate is None:
            try:
                sample_rate = (samples_per_frame *
                               self._get_frame_rate(fh_raw, header0)).to(u.MHz)

            except Exception as exc:
                exc.args += ("the sample rate could not be auto-detected. "
                             "This can happen if the file is too short to "
                             "determine the sample rate, or because it is "
                             "corrupted.  Try passing in an explicit "
                             "`sample_rate`.",)
                raise

        super(VLBIStreamReaderBase, self).__init__(
            fh_raw, header0, sample_shape, bps, complex_data, thread_ids,
            samples_per_frame, sample_rate, squeeze)

    @staticmethod
    def _get_frame_rate(fh, header_template):
        """Returns the number of frames per second.

        Parameters
        ----------
        fh : io.BufferedReader
            Binary file handle.
        header_template : header class or instance
            Definition or instance of file format's header class.

        Returns
        -------
        framerate : `~astropy.units.Quantity`
            Frames per second, in Hz.

        Notes
        -----

        The function cycles through headers, starting from the file pointer's
        current position, to find the next frame whose frame number is zero
        while keeping track of the largest frame number yet found.

        ``_get_frame_rate`` is called when the sample rate is not user-provided
        or deducable from header information.  If less than one second of data
        exists in the file, the function will raise an EOFError.  It also
        returns an error if any header cannot be read or does not verify as
        correct.
        """
        oldpos = fh.tell()
        header = header_template.fromfile(fh)
        frame_nr0 = header['frame_nr']
        sec0 = header.seconds
        while header['frame_nr'] == frame_nr0:
            fh.seek(header.payloadsize, 1)
            header = header_template.fromfile(fh)
        max_frame = frame_nr0
        while header['frame_nr'] > 0:
            max_frame = max(header['frame_nr'], max_frame)
            fh.seek(header.payloadsize, 1)
            header = header_template.fromfile(fh)

        if header.seconds != sec0 + 1:  # pragma: no cover
            warnings.warn("header time changed by more than 1 second?")

        fh.seek(oldpos)
        return (max_frame + 1) * u.Hz

    @lazyproperty
    def _last_header(self):
        """Last header of the file."""
        raw_offset = self.fh_raw.tell()
        self.fh_raw.seek(-self.header0.framesize, 2)
        last_header = self.find_header(template_header=self.header0,
                                       maximum=10*self.header0.framesize,
                                       forward=False)
        self.fh_raw.seek(raw_offset)
        if last_header is None:
            raise ValueError("corrupt VLBI frame? No frame in last {0} bytes."
                             .format(10 * self.header0.framesize))
        return last_header

    @lazyproperty
    def stop_time(self):
        """Time at the end of the file, just after the last sample.

        See also `start_time` for the start time of the file, and `time` for
        the time of the sample pointer's current offset.
        """
        return (self._get_time(self._last_header) +
                (self.samples_per_frame / self.sample_rate).to(u.s))

    @deprecated('0.X', name='time1', alternative='stop_time',
                obj_type='attribute')
    def get_time1(self):
        return self.stop_time

    time1 = property(get_time1, None, None)

    @property
    def size(self):
        """Number of samples in the file."""
        return int(((self.stop_time - self.start_time) *
                    self.sample_rate).to(u.one).round())

    def seek(self, offset, whence=0):
        """Change stream position.

        This works like a normal seek, but the offset is in samples
        (or a relative or absolute time).

        Parameters
        ----------
        offset : int, `~astropy.units.Quantity`, or `~astropy.time.Time`
            Offset to move to.  Can be an (integer) number of samples,
            an offset in time units, or an absolute time.
        whence : int
            Like regular seek, the offset is taken to be from the start if
            ``whence=0`` (default), from the current position if ``1``,
            and from the end if ``2``.  One can use ``'start'``, ``'current'``,
            or ``'end'`` for ``0``, ``1``, or ``2``, respectively.  Ignored if
            ``offset`` is a time.`
        """
        try:
            offset = offset.__index__()
        except Exception:
            try:
                offset = offset - self.start_time
            except Exception:
                pass
            else:
                whence = 0

            offset = int((offset * self.sample_rate).to(u.one).round())

        if whence == 0 or whence == 'start':
            self.offset = offset
        elif whence == 1 or whence == 'current':
            self.offset += offset
        elif whence == 2 or whence == 'end':
            self.offset = self.size + offset
        else:
            raise ValueError("invalid 'whence'; should be 0 or 'start', 1 or"
                             "'current', or 2 or 'end'.")

        return self.offset


class VLBIStreamWriterBase(VLBIStreamBase):
    def close(self):
        extra = self.offset % self.samples_per_frame
        if extra != 0:
            warnings.warn("closing with partial buffer remaining.  "
                          "Writing padded frame, marked as invalid.")
            self.write(np.zeros((self.samples_per_frame - extra,) +
                                self.sample_shape), invalid_data=True)
            assert self.offset % self.samples_per_frame == 0
        return super(VLBIStreamWriterBase, self).close()


default_open_doc = """Open baseband file for reading or writing.

Opened as a binary file, one gets a wrapped file handle that adds
methods to read/write a frame.  Opened as a stream, the handle is
wrapped further, with methods such as read and write access the file
as if it were a stream of samples.

Parameters
----------
name : str or filehandle
    File name or handle
mode : {'rb', 'wb', 'rs', or 'ws'}, optional
    Whether to open for reading or writing, and as a regular binary
    file or as a stream (default is reading a stream).
**kwargs
    Additional arguments when opening the file as a stream.
"""


def make_opener(fmt, classes, doc='', append_doc=True):
    """Create a baseband file opener.

    Parameters
    ----------
    fmt : str
        Name of the baseband format
    classes : dict
        With the file/stream reader/writer classes keyed by names equal to
        'FileReader', 'FileWriter', 'StreamReader', 'StreamWriter' prefixed by
        ``fmt``.  Typically, one will pass in ``classes=globals()``.
    doc : str, optional
        If given, used to define the docstring of the opener.
    append_doc : bool, optional
        If `True` (default), append ``doc`` to the default docstring rather
        than override it.
    """
    module = classes.get('__name__', None)
    classes = {cls_type: classes[fmt + cls_type]
               for cls_type in ('FileReader', 'FileWriter',
                                'StreamReader', 'StreamWriter')}

    def open(name, mode='rs', **kwargs):
        if 'b' in mode:
            cls_type = 'File'
            if kwargs:
                raise TypeError('got unexpected arguments {}'
                                .format(kwargs.keys()))
        else:
            cls_type = 'Stream'

        if 'w' in mode:
            cls_type += 'Writer'
            got_fh = hasattr(name, 'write')
            if not got_fh:
                name = io.open(name, 'w+b')
        elif 'r' in mode:
            cls_type += 'Reader'
            got_fh = hasattr(name, 'read')
            if not got_fh:
                name = io.open(name, 'rb')
        else:
            raise ValueError("only support opening {0} file for reading "
                             "or writing (mode='r' or 'w')."
                             .format(fmt))
        try:
            return classes[cls_type](name, **kwargs)
        except Exception as exc:
            if not got_fh:
                try:
                    name.close()
                except Exception:  # pragma: no cover
                    pass
            raise exc

    open.__doc__ = (default_open_doc.replace('baseband', fmt) + doc
                    if append_doc else doc)
    # This ensures the function becomes visible to sphinx.
    if module:
        open.__module__ = module

    return open
