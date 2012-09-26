import pprint
from copy import deepcopy
from collections import Mapping, MutableMapping


def pretty_print_field(name):
    """ user-agent -> User-Agent """
    return '-'.join(name_pt.capitalize() for name_pt in name.split('-'))


class Headers(MutableMapping):
    """
    Storing headers in an easily accessible way and providing cookielib compatibility
    
    RFC 2616/4.2: Multiple message-header fields with the same field-name MAY be present 
    in a message if and only if the entire field-value for that header field is defined 
    as a comma-separated list.
    """
    __slots__ = 'data',
    
    def __init__(self, *args, **kwargs):
        self.data = dict()
        self.update(*args, **kwargs)        

    def __getitem__(self, name):
        name = name.lower()
        return self.data[name]
            
    def verify_item(self, name, val):
        """ Hook for checking or applying modifications to stored values """
        return str(val)
    
    def __setitem__(self, name, val):
        name = name.lower()
        if isinstance(val, list):
            self.data[name] = [self.verify_item(name, v) for v in val]
        else:
            self.data[name] = [self.verify_item(name, val)]

    def __delitem__(self, name):
        name = name.lower()
        del self.data[name]
        
    def __contains__(self, name):
        # Overwrite for speed instead of relying on MutableMapping
        return name.lower() in self.data
    
    def __iter__(self):
        return self.data.__iter__()
    
    def __len__(self):
        return sum(len(vals) for vals in self.data.values())
    
    def __str__(self):
        return pprint.pformat(self.items())

    def itervalues(self):
        """ Iterates all headers also extracting multiple entries """
        for vals in self.data.values():
            for val in vals:
                yield val
                
    def iteritems(self):
        """ Iterates all headers also extracting multiple entries """
        for name, vals in self.data.items():
            for val in vals:
                yield name, val
                
    def items(self):
        return list(self.iteritems())

    def pretty_items(self):
        for name, val in self.iteritems():
            yield pretty_print_field(name), val
    
    def copy(self):
        return deepcopy(self)
    
    def getheaders(self, name):
        """ Compatibility with urllib/cookielib """
        return self.get(name, [])
    
    getallmatchingheaders = getheaders
    iget = getheaders
    
    def discard(self, name):
        try:
            del self[name]
        except KeyError:
            pass

    def add(self, name, val):
        """ Highlevel replacement for former __setitem__ """
        name = name.lower()
        if name in self:
            if isinstance(val, list):
                self.data[name] += [self.verify_item(name, v) for v in val]
            else:
                self.data[name].append(self.verify_item(name, val))
        else:
            self[name] = val

    def update(*args, **kwds):
        """ Borrowed from MutableMapping to use add instead of __setitem__ """
        if len(args) > 2:
            raise TypeError("update() takes at most 2 positional "
                            "arguments ({} given)".format(len(args)))
        elif not args:
            raise TypeError("update() takes at least 1 argument (0 given)")
        self = args[0]
        other = args[1] if len(args) >= 2 else ()

        if isinstance(other, Mapping):
            for key in other:
                self[key] = other[key]
        elif hasattr(other, "keys"):
            for key in other.keys():
                self[key] = other[key]
        else:
            for key, value in other:
                self.add(key, value)
        for key, value in kwds.items():
            self.add(key, value)

    def compatible_dict(self):
        """ 
        If the client performing the request is not adjusted for this class, this function
        can create a backwards and standards compatible version containing comma joined
        strings instead of lists for multiple headers.
        """
        ret = dict()
        for name in self:
            val = self[name]
            name = pretty_print_field(name)
            if len(val) == 1:
                val = val[0]
            else:
                val = ', '.join(val)
            ret[name] = val
        return ret
    
    
