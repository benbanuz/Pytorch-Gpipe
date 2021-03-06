from typing import Dict, Iterable, Iterator, List, Optional,\
    Tuple, Union, TypeVar, Generic, Callable, Any
import collections
import torch
import torch.nn as nn
from torch import Tensor

__all__ = ["traverse_model", "traverse_params_buffs",
           "find_output_shapes_of_scopes", "model_scopes", "get_device", "_detach_inputs", "_get_size", "_get_shape",
           "Tensors", "TensorsShape", "Devices", "OrderedSet", "layerDict", "tensorDict"]

# the officially supported input types
Tensors = Union[Tensor, List['Tensors'], Tuple['Tensors', ...]]
TensorsShape = Union[torch.Size, Tuple['TensorsShape'], List['TensorsShape']]

Device = Union[torch.device, int, str]
Devices = Union[List[Device], Tuple[Device, ...]]


def traverse_model(model: nn.Module, depth: int = 1000, basic_blocks: Optional[List[nn.Module]] = None, full: bool = False) -> Iterator[Tuple[nn.Module, str, nn.Module]]:
    '''
    iterate over model layers yielding the layer,layer_scope,encasing_module
    Parameters:
    -----------
    model:
        the model to iterate over
    depth:
        how far down in the model tree to go
    basic_blocks:
        a list of modules that if encountered will not be broken down
    full:
        whether to yield only layers specified by the depth and basick_block options or to yield all layers
    '''
    prefix = type(model).__name__
    yield from _traverse_model(model, depth, prefix, basic_blocks, full)


def _traverse_model(module: nn.Module, depth: int, prefix: str, basic_blocks: Optional[Iterable[nn.Module]], full: bool) -> Iterator[Tuple[nn.Module, str, nn.Module]]:
    for name, sub_module in module.named_children():
        scope = prefix + "/" + type(sub_module).__name__ + f"[{name}]"
        if len(list(sub_module.children())) == 0 or (basic_blocks != None and isinstance(sub_module, tuple(basic_blocks))) or depth == 0:
            yield sub_module, scope, module
        else:
            if full:
                yield sub_module, scope, module
            yield from _traverse_model(sub_module, depth - 1, prefix + "/" + type(
                sub_module).__name__ + f"[{name}]", basic_blocks, full)


def model_scopes(model: nn.Module, depth: int = 1000, basic_blocks: Optional[List[nn.Module]] = None, full=False) -> List[str]:
    '''
    return a list of all model scopes for the given configuration
     Parameters:
    -----------
    model:
        the model to iterate over
    depth:
        how far down in the model tree to go
    basic_blocks:
        a list of modules that if encountered will not be broken down
    full:
        whether to return only scopes specified by the depth and basick_block options or to yield all scopes up to them
    '''
    return list(map(lambda t: t[1], traverse_model(model, depth=depth, basic_blocks=basic_blocks, full=full)))


def traverse_params_buffs(module: nn.Module) -> Iterator[Tuple[torch.tensor, str]]:
    '''
    iterate over model's buffers and parameters yielding obj,obj_scope

    Parameters:
    -----------
    model:
        the model to iterate over
    '''
    prefix = type(module).__name__
    yield from _traverse_params_buffs(module, prefix)


def _traverse_params_buffs(module: nn.Module, prefix: str) -> Iterator[Tuple[torch.tensor, str]]:
    # params
    for param_name, param in module.named_parameters(recurse=False):
        param_scope = f"{prefix}/{type(param).__name__}[{param_name}]"
        yield param, param_scope

    # buffs
    for buffer_name, buffer in module.named_buffers(recurse=False):
        buffer_scope = f"{prefix}/{type(buffer).__name__}[{buffer_name}]"
        yield buffer, buffer_scope

    # recurse
    for name, sub_module in module.named_children():
        yield from _traverse_params_buffs(sub_module, prefix + "/" + type(sub_module).__name__ + f"[{name}]")


def find_output_shapes_of_scopes(model, scopes, *sample_batch: Tensors) -> Dict:
    '''
    returns a dictionary from scope to input/output shapes without the batch dimention
    by performing a forward pass

    Parameters:
    -----------
    model:
        the model to profile
    scopes:
        the scopes we wish to know their output shapes
    sample_batch:
        the model sample_batch that will used in the forward pass
    '''
    backup = dict()

    for layer, scope, parent in traverse_model(model, full=True):
        if scope in scopes:
            name = scope[scope.rfind('[') + 1:-1]
            parent.add_module(name, ShapeWrapper(layer))

            new_scope = scope[:scope.rfind('/') + 1] + f"ShapeWrapper[{name}]"
            backup[new_scope] = (scope, name)

    with torch.no_grad():
        model(*sample_batch)
        model.zero_grad()
    scope_to_shape = {}
    for layer, scope, parent in traverse_model(model, full=True):
        if isinstance(layer, ShapeWrapper):
            old_scope, name = backup[scope]
            scope_to_shape[old_scope] = (layer.input_shape, layer.output_shape)
            parent.add_module(name, layer.layer)

    return scope_to_shape


def layerDict(model: nn.Module):
    return {s: l for l, s, _ in traverse_model(model)}


def tensorDict(model: nn.Module):
    return {s: t for t, s in traverse_params_buffs(model)}


INCORRECT_INPUT_TYPE = '''currently supported input types are torch.Tensor, List,Tuple or combination of them found: '''


class ShapeWrapper(nn.Module):
    '''
    a wrapper that when it performs forward pass it records the underlying layer's output shape without batch dimention
    '''

    def __init__(self, sub_module: nn.Module):
        super(ShapeWrapper, self).__init__()
        self.output_shape = []
        self.layer = sub_module
        self.input_shape = []

    def forward(self, *inputs: Tensors):
        self.input_shape = _get_shape(inputs)

        outs = self.layer(*inputs)

        self.output_shape = _get_shape(outs)

        return outs

    # just in case those operations are required we pass them to the profiled layer

    def __iter__(self):
        return iter(self.layer)

    def __getitem__(self, key):
        return self.layer[key]

    def __setitem__(self, key, value):
        self.layer[key] = value

    def __delitem__(self, idx):
        delattr(self.layer, idx)

    def __len__(self):
        return len(self.layer)

    def __contains__(self, key):
        return key in self.layer


def get_device(x: Tensors) -> Device:
    if isinstance(x, torch.Tensor):
        return x.device
    if isinstance(x, (list, tuple)):
        return get_device(x[0])
    raise ValueError(INCORRECT_INPUT_TYPE + f"{type(x)} ")


def _detach_inputs(*inputs: Tensors):
    detached = []
    for x in inputs:
        if isinstance(x, torch.Tensor):
            detached.append(x.detach())
        elif isinstance(x, (list, tuple)):
            tmp = []
            for a in x:
                tmp.append(_detach_inputs(a))
            detached.append(type(x)(tmp))
        else:
            raise ValueError(INCORRECT_INPUT_TYPE + f"{type(x)} ")

    return detached[0] if len(detached) == 1 else tuple(detached)


# for example
# ((5,10,5),(5,55,4)) => ((10,5),(55,4))
def _get_shape(*inputs: Tensors) -> TensorsShape:

    shapes = []
    for x in inputs:
        if isinstance(x, torch.Tensor):
            shapes.append(x.shape[1:])
        elif isinstance(x, (list, tuple)):
            shapes.append(type(x)(_get_shape(*x)))
        else:
            raise ValueError(INCORRECT_INPUT_TYPE + f"{type(x)} ")

    return tuple(shapes)


def _get_size(*inputs: Tensors) -> int:
    size = 0
    for x in inputs:
        if isinstance(x, torch.Tensor):
            size += x.nelement() * x.element_size()
        elif isinstance(x, (list, tuple)):
            for a in x:
                size += _get_size(a)
        else:
            raise ValueError(INCORRECT_INPUT_TYPE + f"{type(x)} ")
    return size


def _count_elements(*inputs: Tensors) -> int:
    c = 0
    for x in inputs:
        if isinstance(x, torch.Tensor):
            c += 1
        elif isinstance(x, (list, tuple)):
            c += _count_elements(*x)
        else:
            raise ValueError(INCORRECT_INPUT_TYPE + f"{type(x)} ")

    return c


T = TypeVar('T')


class OrderedSet(collections.MutableSet, Generic[T]):

    def __init__(self, iterable=None):
        self.end = end = []
        end += [None, end, end]         # sentinel node for doubly linked list
        self.map = {}                   # key --> [key, prev, next]
        if iterable is not None:
            self |= iterable

    def __len__(self):
        return len(self.map)

    def __contains__(self, key):
        return key in self.map

    def add(self, key):
        if key not in self.map:
            end = self.end
            curr = end[1]
            curr[2] = end[1] = self.map[key] = [key, curr, end]
        return self

    def update(self, keys):
        for k in keys:
            self.add(k)
        return self

    def discard(self, key):
        if key in self.map:
            key, prev, next = self.map.pop(key)
            prev[2] = next
            next[1] = prev
        return self

    def __iter__(self) -> Iterator[T]:
        end = self.end
        curr = end[2]
        while curr is not end:
            yield curr[0]
            curr = curr[2]

    def __reversed__(self) -> Iterator[T]:
        end = self.end
        curr = end[1]
        while curr is not end:
            yield curr[0]
            curr = curr[1]

    def pop(self, last=True):
        if not self:
            raise KeyError('set is empty')
        key = self.end[1][0] if last else self.end[2][0]
        self.discard(key)
        return key

    def difference_update(self, other):
        for k in self:
            if k in other:
                self.discard(k)
        return self

    def union(self, *others):
        res = OrderedSet(self.map.keys())
        for s in others:
            assert isinstance(s, (set, OrderedSet))
            res.update(s)
        return res

    def difference(self, *others):
        res = OrderedSet()
        for k in self:
            if not any(k in s for s in others):
                res.add(k)
        return res

    def __repr__(self) -> str:
        if not self:
            return '%s()' % (self.__class__.__name__,)
        return '%s(%r)' % (self.__class__.__name__, list(self))

    def __eq__(self, other) -> bool:
        if isinstance(other, OrderedSet):
            return len(self) == len(other) and list(self) == list(other)
        return set(self) == set(other)

    def indexOf(self, key) -> int:
        for idx, k in enumerate(self):
            if key == k:
                return idx
        return -1

    def __getitem__(self, idx):
        if not isinstance(idx, int):
            raise TypeError(f"expected int index got {type(idx).__name__}")
        if idx < 0 or idx >= len(self):
            raise ValueError("index out of range")

        for i, v in enumerate(self):
            if i == idx:
                return v

        raise Exception("should never happen")


def tensorsMap(f: Callable[[Tensors], Any], tensors: Tensors,):
    """maps each tensor using the f function while keeping the same structure"""

    if isinstance(tensors, torch.Tensor):
        return f(tensors)
    elif isinstance(tensors, (list, tuple)):
        container = type(tensors)
        return container([tensorsMap(f, tensors) for tensors in tensors])
    else:
        raise ValueError(
            f"expected list or tuple or tensor got {tensors.__class__.__name__}")


def batchDim(tensors: Tensors):
    """returns the batch_dim of a Tensors object"""
    if isinstance(tensors, torch.Tensor):
        return tensors.size(0)

    return batchDim(tensors[0])
