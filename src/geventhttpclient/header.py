import pprint
from collections import Mapping, MutableMapping

_dict_setitem = dict.__setitem__
_dict_getitem = dict.__getitem__
_dict_delitem = dict.__delitem__
_dict_contains = dict.__contains__
_dict_setdefault = dict.setdefault


MULTIPLE_HEADERS_ALLOWED = set(['cookie', 'cookie2', 'set-cookie', 'set-cookie2'])


class Headers(dict):
    """ Storing headers in an easily accessible way and providing cookielib compatibility

        RFC 2616/4.2: Multiple message-header fields with the same field-name MAY be present
        in a message if and only if the entire field-value for that header field is defined
        as a comma-separated list.
    """
    def __init__(self, headers=None, **kwargs):
        dict.__init__(self)
        if headers:
            self.update(headers)
        if kwargs:
            self.update(kwargs)

    def __setitem__(self, key, val):
        """ Ensures only lowercase header names
        """
        return _dict_setitem(self, key.lower(), val)

    def __getitem__(self, key):
        return _dict_getitem(self, key.lower())

    def __delitem__(self, key):
        return _dict_delitem(self, key.lower())

    def __contains__(self, key):
        return _dict_contains(self, key.lower())

    def iteritems(self):
        """ Iterates all headers also extracting multiple entries
        """
        for key, vals in dict.iteritems(self):
            if not isinstance(vals, list):
                yield key, vals
            else:
                for val in vals:
                    yield key, val

    def items(self):
        return list(self.iteritems())

    def __len__(self):
        return sum(len(vals) if isinstance(vals, list) else 1 for vals in self.itervalues())

    get = MutableMapping.get

    def add(self, key, val):
        """ Insert new header lines to the container. This method creates lists only for multiple,
            not for single lines. This minimizes the overhead for the common case and optimizes the
            total parsing speed of the headers.
        """
        key = key.lower()
        # Use lower only once and then stick with inbuilt functions for speed
        cur = _dict_setdefault(self, key, val)
        if cur is not val:
            if isinstance(cur, list):
                # Implicitly allowed
                cur.append(val)
            else:
                if key in MULTIPLE_HEADERS_ALLOWED:
                    # Only create duplicate headers for meaningful field names,
                    # else overwrite the field
                    _dict_setitem(self, key, [cur, val])
                else:
                    _dict_setitem(self, key, val)

    def update(self, *args, **kwds):
        """ Adapted from MutableMapping to use self.add instead of self.__setitem__
        """
        if len(args) > 1:
            raise TypeError("update() takes at most one positional "
                            "arguments ({} given)".format(len(args)))
        try:
            other = args[0]
        except IndexError:
            pass
        else:
            if isinstance(other, Mapping):
                for key in other:
                    self.add(key, other[key])
            elif hasattr(other, "keys"):
                for key in other.keys():
                    self.add(key, other[key])
            else:
                for key, value in other:
                    self.add(key, value)

        for key, value in kwds.items():
            self.add(key, value)

    def getheaders(self, key):
        """ Compatibility with urllib/cookielib: Always return lists
        """
        ret = dict.get(self, key.lower(), [])
        if isinstance(ret, list):
            return ret
        else:
            return [ret]

    get = MutableMapping.get
    pop = MutableMapping.pop    
        
    getallmatchingheaders = getheaders
    iget = getheaders

    # Compatibility with urllib3
    getlist = getheaders

    def discard(self, key):
        try:
            self.__delitem__(key)
        except KeyError:
            pass

    @staticmethod
    def _format_field(field):
        return '-'.join(field_pt.capitalize() for field_pt in field.split('-'))

    def pretty_items(self):
        for key, vals in dict.iteritems(self):
            key = self._format_field(key)
            if not isinstance(vals, list):
                yield key, vals
            else:
                for val in vals:
                    yield key, val

    def __str__(self):
        return pprint.pformat(self.pretty_items())
        
    def __repr__(self):
        return "%s(%s)" % (type(self).__name__, self.compatible_dict())

    def copy(self):
        clone = type(self)()
        for key in self:
            val = _dict_getitem(self, key)
            if isinstance(val, list):
                val = list(val)
            _dict_setitem(clone, key, val)
        return clone

    def compatible_dict(self):
        """ If the client performing the request is not adjusted for this class, this function
            can create a backwards and standards compatible version containing comma joined
            strings instead of lists for multiple headers.
        """
        ret = dict()
        for key in self:
            val = self[key]
            key = self._format_field(key)
            if isinstance(val, list):
                val = ', '.join(val)
            ret[key] = val
        return ret

