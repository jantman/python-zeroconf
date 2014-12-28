from __future__ import absolute_import, division, print_function, unicode_literals

""" Multicast DNS Service Discovery for Python, v0.14-wmcbrine
    Copyright 2003 Paul Scott-Murphy, 2014 William McBrine

    This module provides a framework for the use of DNS Service Discovery
    using IP multicast.

    This library is free software; you can redistribute it and/or
    modify it under the terms of the GNU Lesser General Public
    License as published by the Free Software Foundation; either
    version 2.1 of the License, or (at your option) any later version.

    This library is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
    Lesser General Public License for more details.

    You should have received a copy of the GNU Lesser General Public
    License along with this library; if not, write to the Free Software
    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301
    USA
"""

__author__ = 'Paul Scott-Murphy, William McBrine'
__maintainer__ = 'Jakub Stasiak <jakub@stasiak.at>'
__version__ = '0.16.0'
__license__ = 'LGPL'

import enum
import logging
import select
import socket
import struct
import threading
import time
from functools import reduce

import netifaces
from six import indexbytes, int2byte, text_type
from six.moves import xrange


try:
    NullHandler = logging.NullHandler
except AttributeError:
    # Python 2.6 fallback
    class NullHandler(logging.Handler):

        def emit(self, record):
            pass

__all__ = [
    "Zeroconf", "ServiceInfo", "ServiceBrowser",
    "Error", "InterfaceChoice",
]


# hook for threads

_GLOBAL_DONE = False

# Some timing constants

_UNREGISTER_TIME = 125
_CHECK_TIME = 175
_REGISTER_TIME = 225
_LISTENER_TIME = 200
_BROWSER_TIME = 500

# Some DNS constants

_MDNS_ADDR = '224.0.0.251'
_MDNS_PORT = 5353
_DNS_PORT = 53
_DNS_TTL = 60 * 60  # one hour default TTL

_MAX_MSG_TYPICAL = 1460  # unused
_MAX_MSG_ABSOLUTE = 8972

_FLAGS_QR_MASK = 0x8000  # query response mask
_FLAGS_QR_QUERY = 0x0000  # query
_FLAGS_QR_RESPONSE = 0x8000  # response

_FLAGS_AA = 0x0400  # Authorative answer
_FLAGS_TC = 0x0200  # Truncated
_FLAGS_RD = 0x0100  # Recursion desired
_FLAGS_RA = 0x8000  # Recursion available

_FLAGS_Z = 0x0040  # Zero
_FLAGS_AD = 0x0020  # Authentic data
_FLAGS_CD = 0x0010  # Checking disabled

_CLASS_IN = 1
_CLASS_CS = 2
_CLASS_CH = 3
_CLASS_HS = 4
_CLASS_NONE = 254
_CLASS_ANY = 255
_CLASS_MASK = 0x7FFF
_CLASS_UNIQUE = 0x8000

_TYPE_A = 1
_TYPE_NS = 2
_TYPE_MD = 3
_TYPE_MF = 4
_TYPE_CNAME = 5
_TYPE_SOA = 6
_TYPE_MB = 7
_TYPE_MG = 8
_TYPE_MR = 9
_TYPE_NULL = 10
_TYPE_WKS = 11
_TYPE_PTR = 12
_TYPE_HINFO = 13
_TYPE_MINFO = 14
_TYPE_MX = 15
_TYPE_TXT = 16
_TYPE_AAAA = 28
_TYPE_SRV = 33
_TYPE_ANY = 255

# Mapping constants to names

_CLASSES = {_CLASS_IN: "in",
            _CLASS_CS: "cs",
            _CLASS_CH: "ch",
            _CLASS_HS: "hs",
            _CLASS_NONE: "none",
            _CLASS_ANY: "any"}

_TYPES = {_TYPE_A: "a",
          _TYPE_NS: "ns",
          _TYPE_MD: "md",
          _TYPE_MF: "mf",
          _TYPE_CNAME: "cname",
          _TYPE_SOA: "soa",
          _TYPE_MB: "mb",
          _TYPE_MG: "mg",
          _TYPE_MR: "mr",
          _TYPE_NULL: "null",
          _TYPE_WKS: "wks",
          _TYPE_PTR: "ptr",
          _TYPE_HINFO: "hinfo",
          _TYPE_MINFO: "minfo",
          _TYPE_MX: "mx",
          _TYPE_TXT: "txt",
          _TYPE_AAAA: "quada",
          _TYPE_SRV: "srv",
          _TYPE_ANY: "any"}

# utility functions


def current_time_millis():
    """Current system time in milliseconds"""
    return time.time() * 1000

# Exceptions


class Error(Exception):
    pass


class NonLocalNameException(Exception):
    pass


class NonUniqueNameException(Exception):
    pass


class NamePartTooLongException(Exception):
    pass


class AbstractMethodException(Exception):
    pass


class BadTypeInNameException(Exception):
    pass

# implementation classes


class DNSEntry(object):

    """A DNS entry"""

    def __init__(self, name, type, class_):
        self.key = name.lower()
        self.name = name
        self.type = type
        self.class_ = class_ & _CLASS_MASK
        self.unique = (class_ & _CLASS_UNIQUE) != 0

    def __eq__(self, other):
        """Equality test on name, type, and class"""
        return (isinstance(other, DNSEntry) and
                self.name == other.name and
                self.type == other.type and
                self.class_ == other.class_)

    def __ne__(self, other):
        """Non-equality test"""
        return not self.__eq__(other)

    def get_class_(self, class_):
        """Class accessor"""
        return _CLASSES.get(class_, "?(%s)" % class_)

    def get_type(self, t):
        """Type accessor"""
        return _TYPES.get(t, "?(%s)" % t)

    def to_string(self, hdr, other):
        """String representation with additional information"""
        result = "%s[%s,%s" % (hdr, self.get_type(self.type),
                               self.get_class_(self.class_))
        if self.unique:
            result += "-unique,"
        else:
            result += ","
        result += self.name
        if other is not None:
            result += ",%s]" % (other)
        else:
            result += "]"
        return result


class DNSQuestion(DNSEntry):

    """A DNS question entry"""

    def __init__(self, name, type, class_):
        # if not name.endswith(".local."):
        #    raise NonLocalNameException
        DNSEntry.__init__(self, name, type, class_)

    def answered_by(self, rec):
        """Returns true if the question is answered by the record"""
        return (self.class_ == rec.class_ and
                (self.type == rec.type or self.type == _TYPE_ANY) and
                self.name == rec.name)

    def __repr__(self):
        """String representation"""
        return DNSEntry.to_string(self, "question", None)


class DNSRecord(DNSEntry):

    """A DNS record - like a DNS entry, but has a TTL"""

    def __init__(self, name, type, class_, ttl):
        DNSEntry.__init__(self, name, type, class_)
        self.ttl = ttl
        self.created = current_time_millis()

    def __eq__(self, other):
        """Tests equality as per DNSRecord"""
        return isinstance(other, DNSRecord) and DNSEntry.__eq__(self, other)

    def suppressed_by(self, msg):
        """Returns true if any answer in a message can suffice for the
        information held in this record."""
        for record in msg.answers:
            if self.suppressed_by_answer(record):
                return True
        return False

    def suppressed_by_answer(self, other):
        """Returns true if another record has same name, type and class,
        and if its TTL is at least half of this record's."""
        return self == other and other.ttl > (self.ttl / 2)

    def get_expiration_time(self, percent):
        """Returns the time at which this record will have expired
        by a certain percentage."""
        return self.created + (percent * self.ttl * 10)

    def get_remaining_ttl(self, now):
        """Returns the remaining TTL in seconds."""
        return max(0, (self.get_expiration_time(100) - now) / 1000)

    def is_expired(self, now):
        """Returns true if this record has expired."""
        return self.get_expiration_time(100) <= now

    def is_stale(self, now):
        """Returns true if this record is at least half way expired."""
        return self.get_expiration_time(50) <= now

    def reset_ttl(self, other):
        """Sets this record's TTL and created time to that of
        another record."""
        self.created = other.created
        self.ttl = other.ttl

    def write(self, out):
        """Abstract method"""
        raise AbstractMethodException

    def to_string(self, other):
        """String representation with addtional information"""
        arg = "%s/%s,%s" % (self.ttl,
                            self.get_remaining_ttl(current_time_millis()), other)
        return DNSEntry.to_string(self, "record", arg)


class DNSAddress(DNSRecord):

    """A DNS address record"""

    def __init__(self, name, type, class_, ttl, address, logger=None):
        if logger is None:
            self.logger = logging.getLogger(self.__class__.__name__)
        DNSRecord.__init__(self, name, type, class_, ttl)
        self.address = address

    def write(self, out):
        """Used in constructing an outgoing packet"""
        out.write_string(self.address)

    def __eq__(self, other):
        """Tests equality on address"""
        return isinstance(other, DNSAddress) and self.address == other.address

    def __repr__(self):
        """String representation"""
        try:
            return socket.inet_ntoa(self.address)
        except Exception as e:  # TODO stop catching all Exceptions
            self.logger.exception('Unknown error, possibly benign: %r', e)
            return self.address


class DNSHinfo(DNSRecord):

    """A DNS host information record"""

    def __init__(self, name, type, class_, ttl, cpu, os):
        DNSRecord.__init__(self, name, type, class_, ttl)
        self.cpu = cpu
        self.os = os

    def write(self, out):
        """Used in constructing an outgoing packet"""
        out.write_string(self.cpu)
        out.write_string(self.oso)

    def __eq__(self, other):
        """Tests equality on cpu and os"""
        return (isinstance(other, DNSHinfo) and
                self.cpu == other.cpu and self.os == other.os)

    def __repr__(self):
        """String representation"""
        return self.cpu + " " + self.os


class DNSPointer(DNSRecord):

    """A DNS pointer record"""

    def __init__(self, name, type, class_, ttl, alias):
        DNSRecord.__init__(self, name, type, class_, ttl)
        self.alias = alias

    def write(self, out):
        """Used in constructing an outgoing packet"""
        out.write_name(self.alias)

    def __eq__(self, other):
        """Tests equality on alias"""
        return isinstance(other, DNSPointer) and self.alias == other.alias

    def __repr__(self):
        """String representation"""
        return self.to_string(self.alias)


class DNSText(DNSRecord):

    """A DNS text record"""

    def __init__(self, name, type_, class_, ttl, text):
        assert isinstance(text, (bytes, type(None)))
        DNSRecord.__init__(self, name, type_, class_, ttl)
        self.text = text

    def write(self, out):
        """Used in constructing an outgoing packet"""
        out.write_string(self.text)

    def __eq__(self, other):
        """Tests equality on text"""
        return isinstance(other, DNSText) and self.text == other.text

    def __repr__(self):
        """String representation"""
        if len(self.text) > 10:
            return self.to_string(self.text[:7] + "...")
        else:
            return self.to_string(self.text)


class DNSService(DNSRecord):

    """A DNS service record"""

    def __init__(self, name, type, class_, ttl, priority, weight, port, server):
        DNSRecord.__init__(self, name, type, class_, ttl)
        self.priority = priority
        self.weight = weight
        self.port = port
        self.server = server

    def write(self, out):
        """Used in constructing an outgoing packet"""
        out.write_short(self.priority)
        out.write_short(self.weight)
        out.write_short(self.port)
        out.write_name(self.server)

    def __eq__(self, other):
        """Tests equality on priority, weight, port and server"""
        return (isinstance(other, DNSService) and
                self.priority == other.priority and
                self.weight == other.weight and
                self.port == other.port and
                self.server == other.server)

    def __repr__(self):
        """String representation"""
        return self.to_string("%s:%s" % (self.server, self.port))


class DNSIncoming(object):

    """Object representation of an incoming DNS packet"""

    def __init__(self, data):
        """Constructor from string holding bytes of packet"""
        self.offset = 0
        self.data = data
        self.questions = []
        self.answers = []
        self.num_questions = 0
        self.num_answers = 0
        self.num_authorities = 0
        self.num_additionals = 0

        self.read_header()
        self.read_questions()
        self.read_others()

    def unpack(self, format):
        length = struct.calcsize(format)
        info = struct.unpack(format, self.data[self.offset:self.offset + length])
        self.offset += length
        return info

    def read_header(self):
        """Reads header portion of packet"""
        (self.id, self.flags, self.num_questions, self.num_answers,
         self.num_quthorities, self.num_additionals) = self.unpack(b'!6H')

    def read_questions(self):
        """Reads questions section of packet"""
        for i in xrange(self.num_questions):
            name = self.read_name()
            type, class_ = self.unpack(b'!HH')

            question = DNSQuestion(name, type, class_)
            self.questions.append(question)

    def read_int(self):
        """Reads an integer from the packet"""
        return self.unpack(b'!I')[0]

    def read_character_string(self):
        """Reads a character string from the packet"""
        length = indexbytes(self.data, self.offset)
        self.offset += 1
        return self.read_string(length)

    def read_string(self, length):
        """Reads a string of a given length from the packet"""
        info = self.data[self.offset:self.offset + length]
        self.offset += length
        return info

    def read_unsigned_short(self):
        """Reads an unsigned short from the packet"""
        return self.unpack(b'!H')[0]

    def read_others(self):
        """Reads the answers, authorities and additionals section of the
        packet"""
        n = self.num_answers + self.num_authorities + self.num_additionals
        for i in xrange(n):
            domain = self.read_name()
            type, class_, ttl, length = self.unpack(b'!HHiH')

            rec = None
            if type == _TYPE_A:
                rec = DNSAddress(domain, type, class_, ttl, self.read_string(4))
            elif type == _TYPE_CNAME or type == _TYPE_PTR:
                rec = DNSPointer(domain, type, class_, ttl, self.read_name())
            elif type == _TYPE_TXT:
                rec = DNSText(domain, type, class_, ttl, self.read_string(length))
            elif type == _TYPE_SRV:
                rec = DNSService(domain, type, class_, ttl,
                                 self.read_unsigned_short(), self.read_unsigned_short(),
                                 self.read_unsigned_short(), self.read_name())
            elif type == _TYPE_HINFO:
                rec = DNSHinfo(domain, type, class_, ttl,
                               self.read_character_string(), self.read_character_string())
            elif type == _TYPE_AAAA:
                rec = DNSAddress(domain, type, class_, ttl, self.read_string(16))
            else:
                # Try to ignore types we don't know about
                # Skip the payload for the resource record so the next
                # records can be parsed correctly
                self.offset += length

            if rec is not None:
                self.answers.append(rec)

    def is_query(self):
        """Returns true if this is a query"""
        return (self.flags & _FLAGS_QR_MASK) == _FLAGS_QR_QUERY

    def is_response(self):
        """Returns true if this is a response"""
        return (self.flags & _FLAGS_QR_MASK) == _FLAGS_QR_RESPONSE

    def read_utf(self, offset, length):
        """Reads a UTF-8 string of a given length from the packet"""
        return text_type(self.data[offset:offset + length], 'utf-8', 'replace')

    def read_name(self):
        """Reads a domain name from the packet"""
        result = ''
        off = self.offset
        next = -1
        first = off

        while True:
            length = indexbytes(self.data, off)
            off += 1
            if length == 0:
                break
            t = length & 0xC0
            if t == 0x00:
                result = ''.join((result, self.read_utf(off, length) + '.'))
                off += length
            elif t == 0xC0:
                if next < 0:
                    next = off + 1
                off = ((length & 0x3F) << 8) | indexbytes(self.data, off)
                if off >= first:
                    # TODO raise more specific exception
                    raise Exception("Bad domain name (circular) at %s" % (off,))
                first = off
            else:
                # TODO raise more specific exception
                raise Exception("Bad domain name at %s" % (off,))

        if next >= 0:
            self.offset = next
        else:
            self.offset = off

        return result


class DNSOutgoing(object):

    """Object representation of an outgoing packet"""

    def __init__(self, flags, multicast=True):
        self.finished = False
        self.id = 0
        self.multicast = multicast
        self.flags = flags
        self.names = {}
        self.data = []
        self.size = 12

        self.questions = []
        self.answers = []
        self.authorities = []
        self.additionals = []

    def add_question(self, record):
        """Adds a question"""
        self.questions.append(record)

    def add_answer(self, inp, record):
        """Adds an answer"""
        if not record.suppressed_by(inp):
            self.add_answer_at_time(record, 0)

    def add_answer_at_time(self, record, now):
        """Adds an answer if if does not expire by a certain time"""
        if record is not None:
            if now == 0 or not record.is_expired(now):
                self.answers.append((record, now))

    def add_authorative_answer(self, record):
        """Adds an authoritative answer"""
        self.authorities.append(record)

    def add_additional_answer(self, record):
        """Adds an additional answer"""
        self.additionals.append(record)

    def pack(self, format, value):
        self.data.append(struct.pack(format, value))
        self.size += struct.calcsize(format)

    def write_byte(self, value):
        """Writes a single byte to the packet"""
        self.pack(b'!c', int2byte(value))

    def insert_short(self, index, value):
        """Inserts an unsigned short in a certain position in the packet"""
        self.data.insert(index, struct.pack(b'!H', value))
        self.size += 2

    def write_short(self, value):
        """Writes an unsigned short to the packet"""
        self.pack(b'!H', value)

    def write_int(self, value):
        """Writes an unsigned integer to the packet"""
        self.pack(b'!I', int(value))

    def write_string(self, value):
        """Writes a string to the packet"""
        assert isinstance(value, bytes)
        self.data.append(value)
        self.size += len(value)

    def write_utf(self, s):
        """Writes a UTF-8 string of a given length to the packet"""
        utfstr = s.encode('utf-8')
        length = len(utfstr)
        if length > 64:
            raise NamePartTooLongException
        self.write_byte(length)
        self.write_string(utfstr)

    def write_name(self, name):
        """Writes a domain name to the packet"""

        if name in self.names:
            # Find existing instance of this name in packet
            #
            index = self.names[name]

            # An index was found, so write a pointer to it
            #
            self.write_byte((index >> 8) | 0xC0)
            self.write_byte(index & 0xFF)
        else:
            # No record of this name already, so write it
            # out as normal, recording the location of the name
            # for future pointers to it.
            #
            self.names[name] = self.size
            parts = name.split('.')
            if parts[-1] == '':
                parts = parts[:-1]
            for part in parts:
                self.write_utf(part)
            self.write_byte(0)

    def write_question(self, question):
        """Writes a question to the packet"""
        self.write_name(question.name)
        self.write_short(question.type)
        self.write_short(question.class_)

    def write_record(self, record, now):
        """Writes a record (answer, authoritative answer, additional) to
        the packet"""
        self.write_name(record.name)
        self.write_short(record.type)
        if record.unique and self.multicast:
            self.write_short(record.class_ | _CLASS_UNIQUE)
        else:
            self.write_short(record.class_)
        if now == 0:
            self.write_int(record.ttl)
        else:
            self.write_int(record.get_remaining_ttl(now))
        index = len(self.data)
        # Adjust size for the short we will write before this record
        #
        self.size += 2
        record.write(self)
        self.size -= 2

        length = len(b''.join(self.data[index:]))
        self.insert_short(index, length)  # Here is the short we adjusted for

    def packet(self):
        """Returns a string containing the packet's bytes

        No further parts should be added to the packet once this
        is done."""
        if not self.finished:
            self.finished = True
            for question in self.questions:
                self.write_question(question)
            for answer, time_ in self.answers:
                self.write_record(answer, time_)
            for authority in self.authorities:
                self.write_record(authority, 0)
            for additional in self.additionals:
                self.write_record(additional, 0)

            self.insert_short(0, len(self.additionals))
            self.insert_short(0, len(self.authorities))
            self.insert_short(0, len(self.answers))
            self.insert_short(0, len(self.questions))
            self.insert_short(0, self.flags)
            if self.multicast:
                self.insert_short(0, 0)
            else:
                self.insert_short(0, self.id)
        return b''.join(self.data)


class DNSCache(object):

    """A cache of DNS entries"""

    def __init__(self):
        self.cache = {}

    def add(self, entry):
        """Adds an entry"""
        self.cache.setdefault(entry.key, []).append(entry)

    def remove(self, entry):
        """Removes an entry"""
        try:
            list_ = self.cache[entry.key]
            list_.remove(entry)
        except (KeyError, ValueError):
            pass

    def get(self, entry):
        """Gets an entry by key.  Will return None if there is no
        matching entry."""
        try:
            list_ = self.cache[entry.key]
            return list_[list_.index(entry)]
        except (KeyError, ValueError):
            return None

    def get_by_details(self, name, type, class_):
        """Gets an entry by details.  Will return None if there is
        no matching entry."""
        entry = DNSEntry(name, type, class_)
        return self.get(entry)

    def entries_with_name(self, name):
        """Returns a list of entries whose key matches the name."""
        try:
            return self.cache[name]
        except KeyError:
            return []

    def entries(self):
        """Returns a list of all entries"""
        if not self.cache:
            return []
        else:
            return reduce(lambda a, b: a + b, self.cache.values())


class Engine(threading.Thread):

    """An engine wraps read access to sockets, allowing objects that
    need to receive data from sockets to be called back when the
    sockets are ready.

    A reader needs a handle_read() method, which is called when the socket
    it is interested in is ready for reading.

    Writers are not implemented here, because we only send short
    packets.
    """

    def __init__(self, zc, logger=None):
        threading.Thread.__init__(self)
        if logger is None:
            self.logger = logging.getLogger(self.__class__.__name__)
        self.daemon = True
        self.zc = zc
        self.readers = {}  # maps socket to reader
        self.timeout = 5
        self.condition = threading.Condition()
        self.start()

    def run(self):
        while not _GLOBAL_DONE:
            rs = self.get_readers()
            if len(rs) == 0:
                # No sockets to manage, but we wait for the timeout
                # or addition of a socket
                #
                with self.condition:
                    self.condition.wait(self.timeout)
            else:
                try:
                    rr, wr, er = select.select(rs, [], [], self.timeout)
                    for socket_ in rr:
                        try:
                            self.readers[socket_].handle_read(socket_)
                        except Exception as e:  # TODO stop catching all Exceptions
                            self.logger.exception('Unknown error, possibly benign: %r', e)
                except Exception as e:  # TODO stop catching all Exceptions
                    self.logger.exception('Unknown error, possibly benign: %r', e)

    def get_readers(self):
        result = []
        with self.condition:
            result = self.readers.keys()
        return result

    def add_reader(self, reader, socket):
        with self.condition:
            self.readers[socket] = reader
            self.condition.notify()

    def del_reader(self, socket):
        with self.condition:
            del(self.readers[socket])
            self.condition.notify()

    def notify(self):
        with self.condition:
            self.condition.notify()


class Listener(object):

    """A Listener is used by this module to listen on the multicast
    group to which DNS messages are sent, allowing the implementation
    to cache information as it arrives.

    It requires registration with an Engine object in order to have
    the read() method called when a socket is availble for reading."""

    def __init__(self, zc):
        self.zc = zc

    def handle_read(self, socket_):
        try:
            data, (addr, port) = socket_.recvfrom(_MAX_MSG_ABSOLUTE)
        except socket.error as e:
            # If the socket was closed by another thread -- which happens
            # regularly on shutdown -- an EBADF exception is thrown here.
            # Ignore it.
            if e.errno == socket.EBADF:
                return
            else:
                raise e
        self.data = data
        msg = DNSIncoming(data)
        if msg.is_query():
            # Always multicast responses
            #
            if port == _MDNS_PORT:
                self.zc.handle_query(msg, _MDNS_ADDR, _MDNS_PORT)
            # If it's not a multicast query, reply via unicast
            # and multicast
            #
            elif port == _DNS_PORT:
                self.zc.handle_query(msg, addr, port)
                self.zc.handle_query(msg, _MDNS_ADDR, _MDNS_PORT)
        else:
            self.zc.handle_response(msg)


class Reaper(threading.Thread):

    """A Reaper is used by this module to remove cache entries that
    have expired."""

    def __init__(self, zc):
        threading.Thread.__init__(self)
        self.daemon = True
        self.zc = zc
        self.start()

    def run(self):
        while True:
            self.zc.wait(10 * 1000)
            if _GLOBAL_DONE:
                return
            now = current_time_millis()
            for record in self.zc.cache.entries():
                if record.is_expired(now):
                    self.zc.update_record(now, record)
                    self.zc.cache.remove(record)


class ServiceBrowser(threading.Thread):

    """Used to browse for a service of a specific type.

    The listener object will have its add_service() and
    remove_service() methods called when this browser
    discovers changes in the services availability."""

    def __init__(self, zc, type, listener, logger=None):
        """Creates a browser for a specific type"""
        if logger is None:
            self.logger = logging.getLogger(self.__class__.__name__)
        threading.Thread.__init__(self)
        self.daemon = True
        self.zc = zc
        self.type = type
        self.listener = listener
        self.services = {}
        self.next_time = current_time_millis()
        self.delay = _BROWSER_TIME
        self.list = []

        self.done = False

        self.zc.add_listener(self, DNSQuestion(self.type, _TYPE_PTR, _CLASS_IN))
        self.start()

    def update_record(self, zc, now, record):
        """Callback invoked by Zeroconf when new information arrives.

        Updates information required by browser in the Zeroconf cache."""
        if record.type == _TYPE_PTR and record.name == self.type:
            expired = record.is_expired(now)
            try:
                oldrecord = self.services[record.alias.lower()]
                if not expired:
                    oldrecord.reset_ttl(record)
                else:
                    del(self.services[record.alias.lower()])
                    callback = lambda x: self.listener.remove_service(x,
                                                                      self.type, record.alias)
                    self.list.append(callback)
                    return
            except KeyError as e:
                self.logger.debug('Updating newly-found service: %s', record.alias.lower())
                if not expired:
                    self.services[record.alias.lower()] = record
                    callback = lambda x: self.listener.add_service(x,
                                                                   self.type, record.alias)
                    self.list.append(callback)
            expires = record.get_expiration_time(75)
            if expires < self.next_time:
                self.next_time = expires

    def cancel(self):
        self.done = True
        self.zc.notify_all()

    def run(self):
        while True:
            event = None
            now = current_time_millis()
            if len(self.list) == 0 and self.next_time > now:
                self.zc.wait(self.next_time - now)
            if _GLOBAL_DONE or self.done:
                return
            now = current_time_millis()

            if self.next_time <= now:
                out = DNSOutgoing(_FLAGS_QR_QUERY)
                out.add_question(DNSQuestion(self.type, _TYPE_PTR, _CLASS_IN))
                for record in self.services.values():
                    if not record.is_expired(now):
                        out.add_answer_at_time(record, now)
                self.zc.send(out)
                self.next_time = now + self.delay
                self.delay = min(20 * 1000, self.delay * 2)

            if len(self.list) > 0:
                event = self.list.pop(0)

            if event is not None:
                event(self.zc)


class ServiceInfo(object):

    """Service information"""

    def __init__(self, type, name, address=None, port=None, weight=0,
                 priority=0, properties=None, server=None, logger=None):
        """Create a service description.

        type: fully qualified service type name
        name: fully qualified service name
        address: IP address as unsigned short, network byte order
        port: port that the service runs on
        weight: weight of the service
        priority: priority of the service
        properties: dictionary of properties (or a string holding the
                    bytes for the text field)
        server: fully qualified name for service host (defaults to name)"""

        if logger is None:
            self.logger = logging.getLogger(self.__class__.__name__)
        if not name.endswith(type):
            raise BadTypeInNameException
        self.type = type
        self.name = name
        self.address = address
        self.port = port
        self.weight = weight
        self.priority = priority
        if server:
            self.server = server
        else:
            self.server = name
        self._set_properties(properties)

    @property
    def properties(self):
        return self._properties

    def _set_properties(self, properties):
        """Sets properties and text of this info from a dictionary"""
        if isinstance(properties, dict):
            self._properties = properties
            list = []
            result = b''
            for key in properties:
                value = properties[key]
                if isinstance(key, text_type):
                    key = key.encode('utf-8')

                if value is None:
                    suffix = b''
                elif isinstance(value, text_type):
                    suffix = value.encode('utf-8')
                elif isinstance(value, int):
                    if value:
                        suffix = b'true'
                    else:
                        suffix = b'false'
                else:
                    suffix = b''
                list.append(b'='.join((key, suffix)))
            for item in list:
                result = b''.join((result, int2byte(len(item)), item))
            self.text = result
        else:
            self.text = properties

    def _set_text(self, text):
        """Sets properties and text given a text field"""
        self.text = text
        try:
            result = {}
            end = len(text)
            index = 0
            strs = []
            while index < end:
                length = indexbytes(text, index)
                index += 1
                strs.append(text[index:index + length])
                index += length

            for s in strs:
                try:
                    key, value = s.split(b'=', 1)
                    if value == b'true':
                        value = True
                    elif value == b'false' or not value:
                        value = False
                except Exception as e:  # TODO stop catching all Exceptions
                    self.logger.exception('Unknown error, possibly benign: %r', e)
                    # No equals sign at all
                    key = s
                    value = False

                # Only update non-existent properties
                if key and result.get(key) is None:
                    result[key] = value

            self._properties = result
        except Exception as e:  # TODO stop catching all Exceptions
            self.logger.exception('Unknown error, possibly benign: %r', e)
            self._properties = None

    def get_name(self):
        """Name accessor"""
        if self.type is not None and self.name.endswith("." + self.type):
            return self.name[:len(self.name) - len(self.type) - 1]
        return self.name

    def update_record(self, zc, now, record):
        """Updates service information from a DNS record"""
        if record is not None and not record.is_expired(now):
            if record.type == _TYPE_A:
                # if record.name == self.name:
                if record.name == self.server:
                    self.address = record.address
            elif record.type == _TYPE_SRV:
                if record.name == self.name:
                    self.server = record.server
                    self.port = record.port
                    self.weight = record.weight
                    self.priority = record.priority
                    # self.address = None
                    self.update_record(zc, now,
                                       zc.cache.get_by_details(self.server, _TYPE_A, _CLASS_IN))
            elif record.type == _TYPE_TXT:
                if record.name == self.name:
                    self._set_text(record.text)

    def request(self, zc, timeout):
        """Returns true if the service could be discovered on the
        network, and updates this object with details discovered.
        """
        now = current_time_millis()
        delay = _LISTENER_TIME
        next = now + delay
        last = now + timeout
        result = False
        try:
            zc.add_listener(self, DNSQuestion(self.name, _TYPE_ANY, _CLASS_IN))
            while (self.server is None or self.address is None or
                   self.text is None):
                if last <= now:
                    return False
                if next <= now:
                    out = DNSOutgoing(_FLAGS_QR_QUERY)
                    out.add_question(DNSQuestion(self.name, _TYPE_SRV,
                                                 _CLASS_IN))
                    out.add_answer_at_time(zc.cache.get_by_details(self.name,
                                                                   _TYPE_SRV, _CLASS_IN), now)
                    out.add_question(DNSQuestion(self.name, _TYPE_TXT,
                                                 _CLASS_IN))
                    out.add_answer_at_time(zc.cache.get_by_details(self.name,
                                                                   _TYPE_TXT, _CLASS_IN), now)
                    if self.server is not None:
                        out.add_question(DNSQuestion(self.server,
                                                     _TYPE_A, _CLASS_IN))
                        out.add_answer_at_time(zc.cache.get_by_details(self.server,
                                                                       _TYPE_A, _CLASS_IN), now)
                    zc.send(out)
                    next = now + delay
                    delay = delay * 2

                zc.wait(min(next, last) - now)
                now = current_time_millis()
            result = True
        finally:
            zc.remove_listener(self)

        return result

    def __eq__(self, other):
        """Tests equality of service name"""
        if isinstance(other, ServiceInfo):
            return other.name == self.name
        return False

    def __ne__(self, other):
        """Non-equality test"""
        return not self.__eq__(other)

    def __repr__(self):
        """String representation"""
        return '%s(%s)' % (
            type(self).__name__,
            ', '.join(
                '%s=%r' % (name, getattr(self, name))
                for name in (
                    'type', 'name', 'address', 'port', 'weight', 'priority',
                    'server', 'properties',
                )
            )
        )


@enum.unique
class InterfaceChoice(enum.Enum):
    Default = 1
    All = 2


def get_all_addresses(address_family):
    return [
        addr['addr']
        for iface in netifaces.interfaces()
        for addr in netifaces.ifaddresses(iface).get(address_family, [])
    ]


def normalize_interface_choice(choice, address_family):
    if choice is InterfaceChoice.Default:
        choice = ['0.0.0.0']
    elif choice is InterfaceChoice.All:
        # make sure we return only unique values
        choice = list(set(get_all_addresses(address_family)))
    return choice


def new_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # SO_REUSEADDR should be equivalent to SO_REUSEPORT for
    # multicast UDP sockets (p 731, "TCP/IP Illustrated,
    # Volume 2"), but some BSD-derived systems require
    # SO_REUSEPORT to be specified explicity.  Also, not all
    # versions of Python have SO_REUSEPORT available.
    try:
        reuseport = socket.SO_REUSEPORT
    except AttributeError:
        pass
    else:
        s.setsockopt(socket.SOL_SOCKET, reuseport, 1)

    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)

    s.bind(('', _MDNS_PORT))
    return s


class Zeroconf(object):

    """Implementation of Zeroconf Multicast DNS Service Discovery

    Supports registration, unregistration, queries and browsing.
    """

    logger = None
    
    def __init__(
        self,
        interfaces=InterfaceChoice.Default,
        logger=None
    ):
        """Creates an instance of the Zeroconf class, establishing
        multicast communications, listening and reaping threads.

        :type interfaces: :class:`InterfaceChoice` or sequence of ip addresses
        :param logger: a logging object to log to, default None (create own)
        :type logger: :class:`logging.Logger` or None
        """
        if logger is None:
            self.logger = logging.getLogger(self.__class__.__name__)
        
        global _GLOBAL_DONE
        _GLOBAL_DONE = False

        self._listen_socket = new_socket()
        orig_interfaces = interfaces
        interfaces = normalize_interface_choice(interfaces, socket.AF_INET)
        self.logger.debug("normalize_interface_choice({oi}, socket.AF_INET) returns: {i}".format(i=interfaces, oi=orig_interfaces))

        self._respond_sockets = []

        for i in interfaces:
            self.logger.debug("Setting listen sockopts for interface {i}: IPPROTO_IP, IP_ADD_MEMBERSHIP, inet_aton({m}) + inet_aton({i})".format(m=_MDNS_ADDR, i=i))
            self._listen_socket.setsockopt(
                socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(_MDNS_ADDR) + socket.inet_aton(i))

            respond_socket = new_socket()
            respond_socket.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(i))
            self.logger.debug("Setting respond sockopts for interface {i}: IPPROTO_IP, IP_MULTICAST_IF, inet_aton({i}".format(i=i))

            self._respond_sockets.append(respond_socket)

        self.listeners = []
        self.browsers = []
        self.services = {}
        self.servicetypes = {}

        self.cache = DNSCache()

        self.condition = threading.Condition()

        self.logger.debug("Initializing Engine")
        self.engine = Engine(self)
        self.logger.debug("Initializing Listener")
        self.listener = Listener(self)
        self.logger.debug("adding reader to engine")
        self.engine.add_reader(self.listener, self._listen_socket)
        self.logger.debug("Initializing Reaper")
        self.reaper = Reaper(self)

    def wait(self, timeout):
        """Calling thread waits for a given number of milliseconds or
        until notified."""
        with self.condition:
            self.condition.wait(timeout / 1000)

    def notify_all(self):
        """Notifies all waiting threads"""
        with self.condition:
            self.condition.notify_all()

    def get_service_info(self, type, name, timeout=3000):
        """Returns network's service information for a particular
        name and type, or None if no service matches by the timeout,
        which defaults to 3 seconds."""
        info = ServiceInfo(type, name)
        if info.request(self, timeout):
            return info
        return None

    def add_service_listener(self, type, listener):
        """Adds a listener for a particular service type.  This object
        will then have its update_record method called when information
        arrives for that type."""
        self.remove_service_listener(listener)
        self.browsers.append(ServiceBrowser(self, type, listener))

    def remove_service_listener(self, listener):
        """Removes a listener from the set that is currently listening."""
        for browser in self.browsers:
            if browser.listener == listener:
                browser.cancel()
                del(browser)

    def register_service(self, info, ttl=_DNS_TTL):
        """Registers service information to the network with a default TTL
        of 60 seconds.  Zeroconf will then respond to requests for
        information for that service.  The name of the service may be
        changed if needed to make it unique on the network."""
        self.check_service(info)
        self.services[info.name.lower()] = info
        if info.type in self.servicetypes:
            self.servicetypes[info.type] += 1
        else:
            self.servicetypes[info.type] = 1
        now = current_time_millis()
        next_time = now
        i = 0
        while i < 3:
            if now < next_time:
                self.wait(next_time - now)
                now = current_time_millis()
                continue
            out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)
            out.add_answer_at_time(DNSPointer(info.type, _TYPE_PTR,
                                              _CLASS_IN, ttl, info.name), 0)
            out.add_answer_at_time(DNSService(info.name, _TYPE_SRV,
                                              _CLASS_IN, ttl, info.priority, info.weight, info.port,
                                              info.server), 0)
            out.add_answer_at_time(DNSText(info.name, _TYPE_TXT, _CLASS_IN,
                                           ttl, info.text), 0)
            if info.address:
                out.add_answer_at_time(DNSAddress(info.server, _TYPE_A,
                                                  _CLASS_IN, ttl, info.address), 0)
            self.send(out)
            i += 1
            next_time += _REGISTER_TIME

    def unregister_service(self, info):
        """Unregister a service."""
        try:
            del(self.services[info.name.lower()])
            if self.servicetypes[info.type] > 1:
                self.servicetypes[info.type] -= 1
            else:
                del self.servicetypes[info.type]
        except Exception as e:  # TODO stop catching all Exceptions
            self.logger.exception('Unknown error, possibly benign: %r', e)
        now = current_time_millis()
        next_time = now
        i = 0
        while i < 3:
            if now < next_time:
                self.wait(next_time - now)
                now = current_time_millis()
                continue
            out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)
            out.add_answer_at_time(DNSPointer(info.type, _TYPE_PTR,
                                              _CLASS_IN, 0, info.name), 0)
            out.add_answer_at_time(DNSService(info.name, _TYPE_SRV,
                                              _CLASS_IN, 0, info.priority, info.weight, info.port,
                                              info.name), 0)
            out.add_answer_at_time(DNSText(info.name, _TYPE_TXT, _CLASS_IN,
                                           0, info.text), 0)
            if info.address:
                out.add_answer_at_time(DNSAddress(info.server, _TYPE_A,
                                                  _CLASS_IN, 0, info.address), 0)
            self.send(out)
            i += 1
            next_time += _UNREGISTER_TIME

    def unregister_all_services(self):
        """Unregister all registered services."""
        if len(self.services) > 0:
            now = current_time_millis()
            next_time = now
            i = 0
            while i < 3:
                if now < next_time:
                    self.wait(next_time - now)
                    now = current_time_millis()
                    continue
                out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)
                for info in self.services.values():
                    out.add_answer_at_time(DNSPointer(info.type, _TYPE_PTR,
                                                      _CLASS_IN, 0, info.name), 0)
                    out.add_answer_at_time(DNSService(info.name, _TYPE_SRV,
                                                      _CLASS_IN, 0, info.priority, info.weight,
                                                      info.port, info.server), 0)
                    out.add_answer_at_time(DNSText(info.name, _TYPE_TXT,
                                                   _CLASS_IN, 0, info.text), 0)
                    if info.address:
                        out.add_answer_at_time(DNSAddress(info.server,
                                                          _TYPE_A, _CLASS_IN, 0, info.address), 0)
                self.send(out)
                i += 1
                next_time += _UNREGISTER_TIME

    def check_service(self, info):
        """Checks the network for a unique service name, modifying the
        ServiceInfo passed in if it is not unique."""
        now = current_time_millis()
        next_time = now
        i = 0
        while i < 3:
            for record in self.cache.entries_with_name(info.type):
                if (record.type == _TYPE_PTR and
                        not record.is_expired(now) and
                        record.alias == info.name):
                    if info.name.find('.') < 0:
                        info.name = '%s.[%s:%s].%s' % (info.name,
                                                       info.address, info.port, info.type)

                        self.check_service(info)
                        return
                    raise NonUniqueNameException
            if now < next_time:
                self.wait(next_time - now)
                now = current_time_millis()
                continue
            out = DNSOutgoing(_FLAGS_QR_QUERY | _FLAGS_AA)
            self.debug = out
            out.add_question(DNSQuestion(info.type, _TYPE_PTR, _CLASS_IN))
            out.add_authorative_answer(DNSPointer(info.type, _TYPE_PTR,
                                                  _CLASS_IN, _DNS_TTL, info.name))
            self.send(out)
            i += 1
            next_time += _CHECK_TIME

    def add_listener(self, listener, question):
        """Adds a listener for a given question.  The listener will have
        its update_record method called when information is available to
        answer the question."""
        now = current_time_millis()
        self.listeners.append(listener)
        if question is not None:
            for record in self.cache.entries_with_name(question.name):
                if question.answered_by(record) and not record.is_expired(now):
                    listener.update_record(self, now, record)
        self.notify_all()

    def remove_listener(self, listener):
        """Removes a listener."""
        try:
            self.listeners.remove(listener)
            self.notify_all()
        except Exception as e:  # TODO stop catching all Exceptions
            self.logger.exception('Unknown error, possibly benign: %r', e)

    def update_record(self, now, rec):
        """Used to notify listeners of new information that has updated
        a record."""
        for listener in self.listeners:
            listener.update_record(self, now, rec)
        self.notify_all()

    def handle_response(self, msg):
        """Deal with incoming response packets.  All answers
        are held in the cache, and listeners are notified."""
        now = current_time_millis()
        for record in msg.answers:
            expired = record.is_expired(now)
            if record in self.cache.entries():
                if expired:
                    self.cache.remove(record)
                else:
                    entry = self.cache.get(record)
                    if entry is not None:
                        entry.reset_ttl(record)
                        record = entry
            else:
                self.cache.add(record)

            self.update_record(now, record)

    def handle_query(self, msg, addr, port):
        """Deal with incoming query packets.  Provides a response if
        possible."""
        out = None

        # Support unicast client responses
        #
        if port != _MDNS_PORT:
            out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA, False)
            for question in msg.questions:
                out.add_question(question)

        for question in msg.questions:
            if question.type == _TYPE_PTR:
                if question.name == "_services._dns-sd._udp.local.":
                    for stype in self.servicetypes.keys():
                        if out is None:
                            out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)
                        out.add_answer(msg,
                                       DNSPointer("_services._dns-sd._udp.local.",
                                                  _TYPE_PTR, _CLASS_IN, _DNS_TTL, stype))
                for service in self.services.values():
                    if question.name == service.type:
                        if out is None:
                            out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)
                        out.add_answer(msg,
                                       DNSPointer(service.type, _TYPE_PTR,
                                                  _CLASS_IN, _DNS_TTL, service.name))
            else:
                try:
                    if out is None:
                        out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)

                    # Answer A record queries for any service addresses we know
                    if question.type in (_TYPE_A, _TYPE_ANY):
                        for service in self.services.values():
                            if service.server == question.name.lower():
                                out.add_answer(msg, DNSAddress(question.name,
                                                               _TYPE_A, _CLASS_IN | _CLASS_UNIQUE,
                                                               _DNS_TTL, service.address))

                    service = self.services.get(question.name.lower(), None)
                    if not service:
                        continue

                    if question.type in (_TYPE_SRV, _TYPE_ANY):
                        out.add_answer(msg, DNSService(question.name,
                                                       _TYPE_SRV, _CLASS_IN | _CLASS_UNIQUE,
                                                       _DNS_TTL, service.priority, service.weight,
                                                       service.port, service.server))
                    if question.type in (_TYPE_TXT, _TYPE_ANY):
                        out.add_answer(msg, DNSText(question.name,
                                                    _TYPE_TXT, _CLASS_IN | _CLASS_UNIQUE,
                                                    _DNS_TTL, service.text))
                    if question.type == _TYPE_SRV:
                        out.add_additional_answer(DNSAddress(service.server,
                                                             _TYPE_A, _CLASS_IN | _CLASS_UNIQUE,
                                                             _DNS_TTL, service.address))
                except Exception as e:  # TODO stop catching all Exceptions
                    self.logger.exception('Unknown error, possibly benign: %r', e)

        if out is not None and out.answers:
            out.id = msg.id
            self.send(out, addr, port)

    def send(self, out, addr=_MDNS_ADDR, port=_MDNS_PORT):
        """Sends an outgoing packet."""
        packet = out.packet()
        for s in self._respond_sockets:
            bytes_sent = s.sendto(packet, 0, (addr, port))
            if bytes_sent != len(packet):
                raise Error(
                    'Should not happen, sent %d out of %d bytes' % (
                        bytes_sent, len(packet)))

    def close(self):
        """Ends the background threads, and prevent this instance from
        servicing further queries."""
        global _GLOBAL_DONE
        if not _GLOBAL_DONE:
            _GLOBAL_DONE = True
            self.notify_all()
            self.engine.notify()
            self.unregister_all_services()
            for s in [self._listen_socket] + self._respond_sockets:
                s.close()

# Test a few module features, including service registration, service
# query (for Zoe), and service unregistration.

if __name__ == '__main__':
    # if we're calling this directly, setup our logger here
    FORMAT = "[%(levelname)s %(filename)s:%(lineno)s - %(name)s.%(funcName)s() ] %(message)s"
    logging.basicConfig(level=logging.WARN, format=FORMAT, disable_existing_loggers=False)
    logger = logging.getLogger('')
    logger.addHandler(NullHandler())

    if log.level == logging.NOTSET:
        log.setLevel(logging.WARN)

    print("Multicast DNS Service Discovery for Python, version %s" % __version__)
    r = Zeroconf()
    print("1. Testing registration of a service...")
    desc = {'version': '0.10', 'a': 'test value', 'b': 'another value'}
    info = ServiceInfo("_http._tcp.local.",
                       "My Service Name._http._tcp.local.",
                       socket.inet_aton("127.0.0.1"), 1234, 0, 0, desc)
    print("   Registering service...")
    r.register_service(info)
    print("   Registration done.")
    print("2. Testing query of service information...")
    print("   Getting ZOE service: %s" % (
        r.get_service_info("_http._tcp.local.", "ZOE._http._tcp.local.")))
    print("   Query done.")
    print("3. Testing query of own service...")
    info = r.get_service_info("_http._tcp.local.", "My Service Name._http._tcp.local.")
    assert info
    print("   Getting self: %s" % (info,))
    print("   Query done.")
    print("4. Testing unregister of service information...")
    r.unregister_service(info)
    print("   Unregister done.")
    r.close()
