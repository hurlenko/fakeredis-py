import bisect
import itertools
import time
from dataclasses import dataclass
from typing import List, Union, Tuple, Optional, NamedTuple, Dict, Any

from fakeredis._commands import BeforeAny, AfterAny


class StreamEntryKey(NamedTuple):
    ts: int
    seq: int

    def encode(self) -> bytes:
        return f'{self.ts}-{self.seq}'.encode()

    @staticmethod
    def parse_str(entry_key_str: Union[bytes, str]) -> 'StreamEntryKey':
        if isinstance(entry_key_str, bytes):
            entry_key_str = entry_key_str.decode()
        s = entry_key_str.split('-')
        (timestamp, sequence) = (int(s[0]), 0) if len(s) == 1 else (int(s[0]), int(s[1]))
        return StreamEntryKey(timestamp, sequence)


class StreamEntry(NamedTuple):
    key: StreamEntryKey
    fields: List

    def format_record(self):
        results = list(self.fields)
        return [self.key.encode(), results]


def current_time():
    return int(time.time() * 1000)


@dataclass
class StreamConsumerInfo(object):
    name: bytes
    pending: int = 0
    last_attempt: int = current_time()
    last_success: int = current_time()

    def info(self) -> List[bytes]:
        curr_time = current_time()
        return [
            b'name', self.name,
            b'pending', self.pending,
            b'idle', curr_time - self.last_attempt,
            b'inactive', curr_time - self.last_success,
        ]


class StreamGroup(object):
    def __init__(self, stream: 'XStream', name: bytes, start_key: StreamEntryKey, entries_read: int = None):
        self.stream = stream
        self.name = name
        self.start_key = start_key
        self.entries_read = entries_read
        # consumer_name -> #pending_messages
        self.consumers: Dict[bytes, StreamConsumerInfo] = dict()
        self.last_delivered_key = start_key
        self.last_ack_key = start_key
        self.pel = set()  # Pending Entries List, see https://redis.io/commands/xreadgroup/

    def set_id(self, last_delivered_str: bytes, entries_read: Union[int, None]) -> None:
        """Set last_delivered_id for group
        """
        self.start_key = self.stream.parse_ts_seq(last_delivered_str)
        start_index, _ = self.stream.find_index(self.start_key)
        self.entries_read = entries_read
        self.last_delivered_key = self.stream[min(start_index + (entries_read or 0), len(self.stream) - 1)].key

    def add_consumer(self, consumer_name: bytes) -> int:
        if consumer_name in self.consumers:
            return 0
        self.consumers[consumer_name] = StreamConsumerInfo(consumer_name)
        return 1

    def del_consumer(self, consumer_name: bytes) -> int:
        if consumer_name not in self.consumers:
            return 0
        res = self.consumers[consumer_name].pending
        del self.consumers[consumer_name]
        return res

    def consumers_info(self):
        return [self.consumers[k].info() for k in self.consumers]

    def group_info(self) -> List[bytes]:
        start_index, _ = self.stream.find_index(self.start_key)
        last_delivered_index, _ = self.stream.find_index(self.last_delivered_key)
        last_ack_index, _ = self.stream.find_index(self.last_ack_key)
        if start_index + (self.entries_read or 0) > len(self.stream):
            lag = len(self.stream) - start_index - self.entries_read
        else:
            lag = len(self.stream) - 1 - last_delivered_index
        res = {
            b'name': self.name,
            b'consumers': len(self.consumers),
            b'pending': last_delivered_index - last_ack_index,
            b'last-delivered-id': self.last_delivered_key.encode(),
            b'entries-read': self.entries_read,
            b'lag': lag,
        }
        return list(itertools.chain(*res.items()))

    def group_read(self, consumer_name: bytes, start_id: bytes, count: int, noack: bool) -> List:
        if consumer_name not in self.consumers:
            self.consumers[consumer_name] = StreamConsumerInfo(consumer_name)
        if start_id == b'>':
            start_key = self.last_delivered_key
        else:
            start_key = max(StreamEntryKey.parse_str(start_id), self.last_delivered_key)
        items = self.stream.stream_read(start_key, count)
        if not noack:
            self.pel.update({item.key for item in items})
        if len(items) > 0:
            self.last_delivered_key = max(self.last_delivered_key, items[-1].key)
            self.entries_read = (self.entries_read or 0) + len(items)

        consumer = self.consumers[consumer_name]
        consumer.last_attempt = current_time()
        consumer.last_success = current_time()
        consumer.pending += len(items)
        return [x.format_record() for x in items]


class StreamRangeTest:
    """Argument converter for sorted set LEX endpoints."""

    def __init__(self, value: Union[StreamEntryKey, BeforeAny, AfterAny], exclusive: bool):
        self.value = value
        self.exclusive = exclusive

    @staticmethod
    def valid_key(entry_key: Union[bytes, str]) -> bool:
        try:
            StreamEntryKey.parse_str(entry_key)
            return True
        except ValueError:
            return False

    @classmethod
    def decode(cls, value: bytes, exclusive=False):
        if value == b'-':
            return cls(BeforeAny(), True)
        elif value == b'+':
            return cls(AfterAny(), True)
        elif value[:1] == b'(':
            return cls(StreamEntryKey.parse_str(value[1:]), True)
        return cls(StreamEntryKey.parse_str(value), exclusive)


class XStream:
    """Class representing stream.

    The stream contains entries with keys (timestamp, sequence) and field->value pairs.
    This implementation has them as a sorted list of tuples, the first value in the tuple
    is the key (timestamp, sequence).

    Structure of _values list:
    [
       ((timestamp,sequence), [field1, value1, field2, value2, ...])
       ((timestamp,sequence), [field1, value1, field2, value2, ...])
    ]
    """

    def __init__(self):
        self._values: List[StreamEntry] = list()
        self._groups: Dict[bytes, StreamGroup] = dict()
        self._max_deleted_id = StreamEntryKey(0, 0)
        self._entries_added = 0

    def group_get(self, group_name: bytes) -> StreamGroup:
        return self._groups.get(group_name, None)

    def group_add(self, name: bytes, start_key_str: bytes, entries_read: Union[int, None]) -> None:
        """Add a group listening to stream

        :param name: group name
        :param start_key_str: start_key in `timestamp-sequence` format, or $ listen from last.
        :param entries_read: number of entries read.
        """
        if start_key_str == b'$':
            start_key = self._values[len(self._values) - 1].key if len(self._values) > 0 else StreamEntryKey(0, 0)
        else:
            start_key = StreamEntryKey.parse_str(start_key_str)
        self._groups[name] = StreamGroup(self, name, start_key, entries_read)

    def group_delete(self, group_name: bytes) -> int:
        if group_name in self._groups:
            del self._groups[group_name]
            return 1
        return 0

    def groups_info(self) -> List[List[bytes]]:
        res = []
        for group in self._groups.values():
            group_res = group.group_info()
            res.append(group_res)
        return res

    def stream_info(self, full: bool) -> List[bytes]:
        res = {
            b'length': len(self._values),
            b'groups': len(self._groups),
            b'first-entry': self._values[0].format_record() if len(self._values) > 0 else None,
            b'last-entry': self._values[-1].format_record() if len(self._values) > 0 else None,
            b'max-deleted-entry-id': self._max_deleted_id.encode(),
            b'entries-added': self._entries_added,
            b'recorded-first-entry-id': self._values[0].key.encode() if len(self._values) > 0 else b'0-0',
        }
        if full:
            res[b'entries'] = [i.format_record() for i in self._values]
            res[b'groups'] = [g.group_info() for g in self._groups.values()]
        return list(itertools.chain(*res.items()))

    def delete(self, lst: List[Union[str, bytes]]) -> int:
        """Delete items from stream

        :param lst: list of IDs to delete, in the form of `timestamp-sequence`.
        :returns: Number of items deleted
        """
        res = 0
        for item in lst:
            ind, found = self.find_index_key_as_str(item)
            if found:
                self._max_deleted_id = max(self._values[ind].key, self._max_deleted_id)
                del self._values[ind]
                res += 1
        return res

    def add(self, fields: List, entry_key: str = '*') -> Union[None, bytes]:
        """Add entry to a stream.

        If the entry_key can not be added (because its timestamp is before the last entry, etc.),
        nothing is added.

        :param fields: list of fields to add, must [key1, value1, key2, value2, ... ]
        :param entry_key:
            key for the entry, formatted as 'timestamp-sequence'
            If entry_key is '*', the timestamp will be calculated as current time and the sequence based
            on the last entry key of the stream.
            If entry_key is 'ts-*', and the timestamp is greater or equal than the last entry timestamp,
            then the sequence will be calculated accordingly.
        :returns:
            The key of the added entry.
            None if nothing was added.
        :raises AssertionError: if len(fields) is not even.
        """
        assert len(fields) % 2 == 0
        if isinstance(entry_key, bytes):
            entry_key = entry_key.decode()

        if entry_key is None or entry_key == '*':
            ts, seq = int(1000 * time.time()), 0
            if (len(self._values) > 0
                    and self._values[-1].key.ts == ts
                    and self._values[-1].key.seq >= seq):
                seq = self._values[-1][0].seq + 1
            ts_seq = StreamEntryKey(ts, seq)
        elif entry_key[-1] == '*':  # entry_key has `timestamp-*` structure
            split = entry_key.split('-')
            if len(split) != 2:
                return None
            ts, seq = int(split[0]), split[1]
            if len(self._values) > 0 and ts == self._values[-1].key.ts:
                seq = self._values[-1].key.seq + 1
            else:
                seq = 0
            ts_seq = StreamEntryKey(ts, seq)
        else:
            ts_seq = StreamEntryKey.parse_str(entry_key)

        if len(self._values) > 0 and self._values[-1].key > ts_seq:
            return None
        entry = StreamEntry(ts_seq, list(fields))
        self._values.append(entry)
        self._entries_added += 1
        return entry.key.encode()

    def __len__(self):
        return len(self._values)

    def __iter__(self):
        def gen():
            for record in self._values:
                yield record.format_record()

        return gen()

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._values[item]
        return None

    def find_index(self, entry_key: StreamEntryKey, from_left=True) -> Tuple[int, bool]:
        """Find the closest index to entry_key_str in the stream
        :param entry_key: key for the entry.
        :param from_left: if not found exact match, return index of last smaller element
        :returns: A tuple of
            ( index of entry with the closest (from the left) key to entry_key_str,
              Whether the entry key is equal )
        """
        if len(self._values) == 0:
            return 0, False
        if from_left:
            ind = bisect.bisect_left(list(map(lambda x: x.key, self._values)), entry_key)
        else:
            ind = bisect.bisect_right(list(map(lambda x: x.key, self._values)), entry_key)
        return ind, (ind < len(self._values) and self._values[ind].key == entry_key)

    def find_index_key_as_str(self, entry_key_str: Union[str, bytes]) -> Tuple[int, bool]:
        """Find the closest index to entry_key_str in the stream
        :param entry_key_str: key for the entry, formatted as 'timestamp-sequence'.
        :returns: A tuple of
            ( index of entry with the closest (from the left) key to entry_key_str,
              Whether the entry key is equal )
        """
        if entry_key_str == b'$':
            return max(len(self._values) - 1, 0), True
        ts_seq = StreamEntryKey.parse_str(entry_key_str)
        return self.find_index(ts_seq)

    @staticmethod
    def parse_ts_seq(ts_seq_str: Union[str, bytes]) -> StreamEntryKey:
        if ts_seq_str == b'$':
            return StreamEntryKey(0, 0)
        return StreamEntryKey.parse_str(ts_seq_str)

    def trim(self,
             max_length: Optional[int] = None,
             start_entry_key: Optional[str] = None,
             limit: Optional[int] = None) -> int:
        """Trim a stream

        :param max_length: max length of resulting stream after trimming (number of last values to keep)
        :param start_entry_key: min entry-key to keep, can not be given together with max_length.
        :param limit: number of entries to keep from minid.
        :returns: The resulting stream after trimming.
        :raises ValueError: When both max_length and start_entry_key are passed.
        """
        if max_length is not None and start_entry_key is not None:
            raise ValueError('Can not use both max_length and start_entry_key')
        start_ind = None
        if max_length is not None:
            start_ind = len(self._values) - max_length
        elif start_entry_key is not None:
            ind, exact = self.find_index_key_as_str(start_entry_key)
            start_ind = ind
        res = max(start_ind, 0)
        if limit is not None:
            res = min(start_ind, limit)
        self._values = self._values[res:]
        return res

    def irange(self, start: StreamRangeTest, stop: StreamRangeTest, reverse=False) -> List[Any]:
        """Returns a range of the stream from start to stop.

        :param start: start key
        :param stop: stop key
        :param reverse: Should the range be in reverse order?
        :returns: the range between start and stop
        """

        def _find_index(elem: StreamRangeTest, from_left=True) -> int:
            if isinstance(elem.value, BeforeAny):
                return 0
            if isinstance(elem.value, AfterAny):
                return len(self._values)
            ind, found = self.find_index(elem.value, from_left)
            if found and elem.exclusive:
                ind += 1
            return ind

        start_ind = _find_index(start)
        stop_ind = _find_index(stop, from_left=False)
        matches = map(lambda x: self._values[x].format_record(), range(start_ind, stop_ind))
        if reverse:
            return list(reversed(tuple(matches)))
        return list(matches)

    def last_item_key(self) -> bytes:
        return self._values[-1].key.encode() if len(self._values) > 0 else '0-0'.encode()

    def stream_read(self, start_key: StreamEntryKey, count: Union[int, None]) -> List[StreamEntry]:
        start_ind, found = self.find_index(start_key)
        if found:
            start_ind += 1
        if start_ind >= len(self):
            return []
        end_ind = len(self) if count is None or start_ind + count >= len(self) else start_ind + count
        return self._values[start_ind:end_ind]
