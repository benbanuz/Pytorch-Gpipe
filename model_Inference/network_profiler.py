import torch
import torch.nn as nn
import timeit


class Wrapper(nn.Module):
    '''
    a module whose purpose is to profile a given layer,
    measuring forward/backward computation time, and estimated memory consumption
    '''

    def __init__(self, sub_module: nn.Module, idx, device):
        super(Wrapper, self).__init__()
        self.layer = sub_module
        self.device = device
        self.idx = idx
        self.forward_time = None
        self.backward_timestamp = None

        def backward_time_hook(_, __, ___):
            # record when the layer finished calculating gradients
            if self.device == "cuda":
                torch.cuda.synchronize()
            self.backward_timestamp = timeit.time.time()

        self.layer.register_backward_hook(backward_time_hook)

        self.size = self._layer_size()

    def _layer_size(self):
        '''
        return the size of the layer considering parameters and buffers
        '''
        # TODO maybe include activations/gradients size
        size = 0
        for param in self.layer.parameters():
            size += param.nelement() * param.element_size()
        for buffer in self.layer.buffers():
            size += buffer.nelement() * buffer.element_size()

        return size

    def forward(self, x):
        '''
        measures the time in milliseconds it took for the module to complete forward computation
        '''
        forward_time = 0
        out = None
        if(x.is_cuda):
            # milliseconds
            torch.cuda.synchronize()
            start = torch.cuda.Event(enable_timing=True)
            end = torch.cuda.Event(enable_timing=True)
            start.record()
            out = self.layer(x)
            end.record()
            torch.cuda.synchronize()
            forward_time = 0
            forward_time = (start.elapsed_time(end))
        else:
            # convert to milliseconds
            start = timeit.time.time()
            out = self.layer(x)
            end = timeit.time.time()
            forward_time = 1000*(end - start)

        self.forward_time = forward_time

        return out


class NetProfiler(nn.Module):
    '''
    a module who profiles a network's computation time(forward/backward) and memory consumption
    done via wrapping all layers of the network with a special Wrapper module
    '''
    # TODO maybe include activations/gradients size
    # TODO find better way to measure the backward computation time

    def __init__(self, module, sample_input, device="cuda"):
        super(NetProfiler, self).__init__()
        self.network = module
        self.num_layers = None
        self.layers = None
        self.device = device
        self._backward_times = None
        self._forward_times = None

        self._profile(sample_input)

    def forward(self, x):
        out = x.to(self.device)
        self.to(self.device)
        # warmup
        a = torch.randn(1000, 2000).to(self.device)
        b = torch.randn(2000, 1000).to(self.device)
        a.mm(b)
        if self.device == "cuda":
            torch.cuda.synchronize()

        out = self.network(out)

        return out

    def _gather_backward_times(self):
        # time is calculated by subtracting successive timestamps
        # for lack of a better option the last layer performs backpropagation in 0 time
        self._backward_times = [
            layer.backward_timestamp for layer in self.layers.values()]

        self._backward_times = [
            end-start for end, start in zip(self._backward_times, self._backward_times[1:])]+[0]

    def _wrap_individual_layers(self, module: nn.Module, idx, layers_dict):
        '''
        wraps all layers of module by changing the binding in the network module dictionary 
        '''
        for name, sub_module in module._modules.items():
            # assume no cyclic routes in the network
            # a module with no children is a layer
            if len(list(sub_module.children())) == 0:
                module._modules[name] = Wrapper(sub_module, idx, self.device)
                layers_dict[idx] = module._modules[name]
                idx += 1
            else:
                idx, layers_dict = self._wrap_individual_layers(
                    sub_module, idx, layers_dict)
        return idx, layers_dict

    def _profile(self, sample_input):
        '''
        profiles the network using a sample input
        '''
        # wrap all individula layers for profiling
        self.num_layers, self.layers = self._wrap_individual_layers(
            self.network, 0, {})

        # gather all individual layer sizes
        self._layer_sizes = [layer.size for layer in self.layers.values()]

        # perform symbolic forward run
        if self.device == "cuda":
            torch.cuda.synchronize()

        out = self(sample_input)

        if self.device == "cuda":
            torch.cuda.synchronize()

        # gather forward times results
        self._forward_times = [
            layer.forward_time for layer in self.layers.values()]

        # perform symbolic backward run
        loss = out.norm()
        if self.device == "cuda":
            torch.cuda.synchronize()

        loss.backward()

        if self.device == "cuda":
            torch.cuda.synchronize()

        # gather backward times results
        self._gather_backward_times()
