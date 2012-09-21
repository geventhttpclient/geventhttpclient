import pprint
from copy import deepcopy
from collections import MutableMapping


def pretty_print_field(name):
    """ user-agent -> User-Agent """
    return '-'.join(name_pt.capitalize() for name_pt in name.split('-'))


class Headers(MutableMapping):
    """ Storing headers in an easily accessible way and providing cookielib compatibility """

    __slots__ = 'data',

    def __init__(self, *args, **kwargs):
        self.data = dict()
        self.update(*args, **kwargs)

    def __getitem__(self, name):
        name = name.lower()
        return self.data[name]
            
    def verify_item(self, name, val):
        """ Hook for checking or applying modifications to stored values """
        return val
    
    def __setitem__(self, name, val):
        name = name.lower()
        val = self.verify_item(name, val)
        if isinstance(val, list):
            if name in self.data:
                self.data[name] += val
            else:
                self.data[name] = val
        else:
            if name in self.data:
                self.data[name].append(val)
            else:
                self.data[name] = [val]
            
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
    
    def copy(self):
        return deepcopy(self)
    
    def __str__(self):
        return pprint.pformat(self.items())
    
