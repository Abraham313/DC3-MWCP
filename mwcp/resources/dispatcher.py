"""
Implements a data pump for extracted file data which allows for
more robust file identification, reporting, and objectifying
content to ease maintenance.
"""

import hashlib
import io
import os
import traceback
from collections import deque

import pefile
from mwcp.utils import pefileutils


class UnableToParse(Exception):
    """
    This exception can be thrown if a parser that has been correctly identified has failed to parse
    the file and you would like other parsers to be tried.
    """
    pass


class FileObject(object):
    """
    This class represents a file object which is to be parsed by the MWCP parser.  It is pushed into the dispatcher
    queue for processing.
    """

    def __init__(
            self, file_data, reporter, pe=None, file_name=None, def_stub=None,
            description=None, output_file=True, use_supplied_fname=True, use_arch=False):
        """
        Initializes the FileObject.

        :param bytes file_data: Data for the file.
        :param pefile.PE pe: PE object for the file.
        :param mwcp.Reporter reporter: MWCP reporter.
        :param str file_name: File name to use if file is not a PE or use_supplied_fname was specified.
        :param str description: Description of the file object.
        :param bool output_file: Boolean indicating if file should be outputted when the dispatcher process the file.
        :param bool use_supplied_fname: Boolean indicating if the file_name should be used even if the file is a PE.
        :param str def_stub: def_stub argument to pass to obtain_original_filename()
        :param bool use_arch: use_arch argument to pass to obtain_original_filename()
        """
        self._file_path = None
        self._md5 = None
        self._stack_strings = None
        self._resources = None
        self.output_file = output_file
        self._outputted_file = False
        self.parent = None   # Parent FileObject from which FileObject was extracted from (this is set externally).
        self.parser = None   # This will be set by the dispatcher.
        self.file_data = file_data
        self.reporter = reporter
        self.description = description

        self.pe = pe or pefileutils.obtain_pe(file_data)

        use_supplied_fname = use_supplied_fname or not self.pe

        if file_name and use_supplied_fname:
            self.file_name = file_name
        else:
            self.file_name = pefileutils.obtain_original_filename(
                def_stub or self.md5.encode('hex'), pe=self.pe, reporter=reporter, use_arch=use_arch)

        # Sanity check
        assert self.file_name

    def __enter__(self):
        """
        This allows us to use the file_data as a file-like object when used as a context manager.

        e.g.
            >>> file_object = FileObject('hello world', None)
            >>> with file_object as fo:
            ...     _ = fo.seek(6)
            ...     print fo.read()
            world
        """
        self._open_file = io.BytesIO(self.file_data)
        return self._open_file

    def __exit__(self, *args):
        self._open_file.close()

    @property
    def parser_history(self):
        """
        Returns a history of the parser classes (including current) that has lead to the creation of the file object.
        e.g. [MalwareDropper, MalwareLoader, MalwareImplant]
        :return list: List of parser classes.
        """
        history = [self.parser]
        parent = self.parent
        while parent:
            history.append(parent.parser)
            parent = parent.parent
        return reversed(history)

    @property
    def md5(self):
        """
        Returns md5 hash of file.
        :return: The md5 hash of the file as a byte string.
        """
        if not self._md5:
            self._md5 = hashlib.md5(self.file_data).digest()
        return self._md5

    @property
    def file_path(self):
        """
        Returns a full file path to the file object.
        This is useful for when you want to use this file on libraries which require
        a file path instead of data or file-like object (e.g. cabinet).
        Always create a temporary file, this avoids issues where the identify function requires the file_path and
        the file would be output before a description is set.
        """
        if not self._file_path:
            file_path = os.path.join(self.reporter.managed_tempdir(), self.file_name)
            with open(file_path, 'wb') as file_object:
                file_object.write(self.file_data)
            self._file_path = file_path

        return self._file_path

    @file_path.setter
    def file_path(self, value):
        """
        Setter for the file_path attribute. This is used if an external entity can
        provided a valid file_path.
        """
        self._file_path = value

    @property
    def resources(self):
        """Returns a list of the PE resources for the given file."""
        if self.pe and not self._resources:
            self._resources = list(pefileutils.iter_rsrc(self.pe))
        return self._resources

    def output(self):
        """
        Outputs FileObject instance to reporter it it hasn't already been outputted.
        """
        # Output file if we are allowed to and the file hasn't already been outputted.
        if self.output_file and not self._outputted_file:
            # TODO: There is the chance that two unique files have the same filename, unfortunately the
            # Reporter assumes all outputted filenames are unique, so there is nothing we can do.
            if self.file_name not in self.reporter.outputfiles:
                self.reporter.output_file(
                    data=self.file_data, filename=self.file_name or '', description=self.description or '')
                self._outputted_file = True


class ComponentParser(object):
    """
    This is a templated base class for all parser objects.  Either use this as a base for all component parsers, or
    inherit this class into a customized base class for all parsers.  This class includes some of the required data
    used by various other classes.
    """

    # This is the description that will be given the the file object during output
    # if no description is set in the file_object. This must be overwritten by inherited classes.
    DESCRIPTION = None

    def __init__(self, file_object, reporter, dispatcher):
        super(ComponentParser, self).__init__()
        self.file_object = file_object
        self.reporter = reporter
        self.dispatcher = dispatcher
        self.kordesii_reporter = None
        if not self.DESCRIPTION:
            raise NotImplementedError('Parser class is missing a DESCRIPTION.')

    @classmethod
    def identify(cls, file_object):
        """
        Determines if this parser is identified to support the given file_object.
        This function must be overwritten in order to support identification.

        The passed in file_object may be modified at this time to provide
        a new file_name or description.
        (Be aware, that this change will be in affect for future parsers.
        Therefore, don't change it if you are returning False or the dispatcher is in greedy mode.)

        :param file_object: file object to use for identification
        :type file_object: dispatcher.FileObject

        :return bool: Boolean indicating if this parser supports the file_object
        """
        raise NotImplementedError

    def _debug_msg(self, msg):
        """
        Helper function to output a debug string which will format using only the file name.
        :param msg: message to be output
        :return:
        """
        self.reporter.debug(msg.format(self.file_object.file_name))

    def run(self):
        """
        This function can be overwritten. It is called by the dispatcher to run the component parser.
        You don't have to overwrite this method if you only want to identify/output the file.
        :return:
        """
        pass


class UnidentifiedFile(ComponentParser):
    """Describes an unidentified file. This parser will hit on any FileObject."""
    DESCRIPTION = 'Unidentified file'

    @classmethod
    def identify(cls, file_object):
        """
        Identifies an unidentified file... which means this is always True.

        :param file_object: dispatcher.FileObject object
        :return: Boolean indicating idenification
        """
        return True


class Dispatcher(object):
    """
    This class will continuously process items that are in the queue.  When the queue is empty,
    this will ultimately signal that processing is complete and the script will terminate.
    This class will process the items using the supplied list of Parser classes provided.

    This class can be used as a mixin along with the Parser class or
    can be initialized by itself.
    When used as a mixin, the dispatcher will automatically add the file in the reporter
    to the queue and run dispatch() when run() is called.

    If using as a mixin you must define this class first before Parser
    and make sure you call the __init__ for both classes.
    For example:

        from mwcp import Dispatcher, FileObject, Parser

        class SuperMalwareParser(Dispatcher, Parser):
            def __init__(self, reporter):
                Parser.__init__(
                    self,
                    description='Module for SuperMalware',
                    author='DCFL'
                    reporter=reporter)
                Dispatcher.__init__(
                    self,
                    reporter=reporter,
                    parsers=[SuperMalware_Loader, SuperMalware_Implant])

    (NOTE: The run() function DOES NOT need to be implemented if using it in this way.
    The Dispatcher's run() will be used.)

    If not using as a mixin you'll need to initialize the dispatcher and run the dispatcher yourself.
    For example:

        from mwcp import Dispatcher, FileObject, Parser

        class SuperMalwareParser(Parser):
            def __init__(self, reporter):
                Parser.__init__(
                    self,
                    description='Module for SuperMalware',
                    author='DCFL'
                    reporter=reporter)

            def run(self):
                input_file = FileObject(
                    file_data=self.reporter.data,
                    pe=self.reporter.pe,
                    file_name=os.path.basename(self.reporter.filename()),
                    use_supplied_fname=True)
                dispatcher = Dispatcher(reporter, [SuperMalware_Loader, SuperMalware_Implant])
                dispatcher.add_to_queue(input_file)
                dispatcher.dispatch()

    """

    def __init__(self, reporter, parsers=None, greedy=False, default=UnidentifiedFile):
        """
        Initializes the Dispatcher with the given reporter and parsers to run.

        :param reporter: An MWCP reporter.
        :param parsers: A list of parser classes to use for detection and running.
            Order of this list is the order the Dispatcher will perform its identification.
            If not provided, it will default to an empty list. (which is not very useful unless you
            plan to overwrite the _identify_file() function.)
        :param greedy: By default, the dispatcher will only run on the first parser it detects
            to be a valid parser. If greedy is set to true, the dispatcher will try all parsers
            even if a previous parser was successful.
        :param default: The Parser class to default to if no parsers in the parsers list
            has identified it. If set to None, no parser will be run as default.
            (By default, the dispatcher.UnidentifiedFile will be run.)
        """
        self.reporter = reporter
        self.parsers = parsers or []
        self.greedy = greedy
        self.default = default
        self._fifo_buffer = deque()
        self._current_file_object = None
        self._current_parser_class = None

        # Dictionary that can be used by parsers to pass variables across parsers.
        # E.g. an encryption key found in the loader to be used by the implant.
        self.knowledge_base = {}

    def run(self):
        """
        Entry point into parser, called by MWCP framework.
        If this class is used as a mixin along with the MWCP framework
        this function can be used as the entry point into the mwcp framwork.
        """
        # Add and run dispatcher with starting file found in reporter.
        self.reporter.debug('[*] Configuration parsing started.')
        input_file = FileObject(file_data=self.reporter.data,
                                pe=self.reporter.pe,
                                reporter=self.reporter,
                                file_name=os.path.basename(self.reporter.filename()),
                                output_file=False)
        # Use the input file path as the file_path attribute.
        input_file.file_path = self.reporter.filename()
        self.add_to_queue(input_file)
        self.dispatch()

    def add_to_queue(self, file_object):
        """
        Add a FileObject to the FIFO queue for processing.
        :param file_object: a FileObject object requiring processing.
        :return:
        """
        assert isinstance(file_object, FileObject)
        file_object.parent = self._current_file_object
        self._fifo_buffer.appendleft(file_object)
        if self._current_file_object:
            self.reporter.debug(
                '[*] {} dispatched residual file: {}'.format(self._current_file_object.file_name, file_object.file_name))

    def _identify_file(self, file_object):
        """
        Generator that detects which parsers to run based on given file_object.
        This function can be overwritten if you need to change the detection algorithm.

        :param FileObject file_object: file object that needs to be identified
        :rtype ParserBase: parser class to us to process the identified file
        """
        identified = False
        for parser_class in self.parsers:
            if parser_class.identify(file_object):
                self.reporter.debug(
                    '[*] File {} identified as {}.'.format(file_object.file_name, parser_class.DESCRIPTION))
                identified = True
                yield parser_class

        if not identified:
            # If no parsers match and developer didn't set a description, mark as unidentified file.
            if not file_object.description:
                self.reporter.debug('[*] Supplied file {} was not identified.'.format(file_object.file_name))
            if self.default:
                yield self.default

    def dispatch(self):
        """
        This function will continue processing until the queue is empty.
        """
        while self._fifo_buffer:
            file_object = self._fifo_buffer.pop()

            # Run any applicable parsers.
            for parser_class in self._identify_file(file_object):
                self._current_file_object = file_object
                self._current_parser_class = parser_class

                # If a description wasn't set for the file, use the parser's
                if not file_object.description:
                    file_object.description = parser_class.DESCRIPTION

                # Set parser class used in order to keep a history.
                file_object.parser = parser_class

                try:
                    parser = parser_class(file_object, self.reporter, self)
                    parser.run()

                except UnableToParse as exception:
                    self.reporter.debug(
                        '[*] File {} was miss-identified as {}, due to: ({}) '
                        'Trying other parsers...'.format(file_object.file_name, parser_class.DESCRIPTION, exception))
                    continue

                except Exception:
                    self.reporter.debug(
                        '[*] {} dispatch parser failed with error:\n{}'.format(
                            parser_class.__name__, traceback.format_exc()))

                if not self.greedy:
                    break

            # Output the file.
            # NOTE: We don't want to output the file until the very end, since a parser may want to change
            # the file's filename or description.
            file_object.output()