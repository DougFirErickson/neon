# ----------------------------------------------------------------------------
# Copyright 2014 Nervana Systems Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ----------------------------------------------------------------------------
import numpy as np
from operator import add

from neon import NervanaObject
from neon.layers.layer import Layer, BranchNode, Dropout, DataTransform, Convolution
from neon.util.persist import load_class


def flatten(item):
    if hasattr(item, '__iter__'):
        for i in iter(item):
            for j in flatten(i):
                yield j
    else:
        yield item


class LayerContainer(Layer):
    """
    Layer containers are a generic class that are used to encapsulate groups of layers and
    provide methods for propagating through the constituent layers, allocating memory
    """
    @property
    def layers_to_optimize(self):
        lto = []
        for l in self.layers:
            if isinstance(l, LayerContainer):
                lto += l.layers_to_optimize
            elif l.has_params:
                lto.append(l)
        return lto

    def nested_str(self, level=0):
        padstr = '\n' + '  '*level
        ss = '  ' * level + self.classnm + padstr
        ss += padstr.join([l.nested_str(level+1) for l in self.layers])
        return ss

    @classmethod
    def gen_class(cls, pdict):
        layers = []
        for layer in pdict['layers']:
            typ = layer['type']
            if typ.find(__name__) != -1:
                # this is a sequential layer
                ccls = load_class(typ)
            elif typ in globals():
                # this may occur if full path not given
                # be careful here because globals has stuff from outside this module
                ccls = globals()[typ]
            else:
                # look in neon.layers.layer
                if typ.find('neon.layers.layer') == -1:
                    typ = 'neon.layers.layer.' + typ
                ccls = load_class(typ)
            layers.append(ccls.gen_class(layer['config']))

        # layers is special in that there may be parameters
        # serialzed which will be used elsewhere
        lsave = pdict.pop('layers')
        new_cls = cls(layers=layers, **pdict)
        pdict['layers'] = lsave
        return new_cls

    def get_description(self, get_weights=False, keep_states=False):
        desc = super(LayerContainer, self).get_description(skip=['layers'])
        desc['container'] = True
        desc['config']['layers'] = []
        for layer in self.layers:
            desc['config']['layers'].append(layer.get_description(get_weights=get_weights,
                                                                  keep_states=keep_states))
        self._desc = desc
        return desc

    def load_weights(self, pdict, inference=False):
        assert len(pdict['config']['layers']) == len(self.layers)
        for branch, bdict in zip(self.layers, pdict['config']['layers']):
            branch.load_weights(bdict, inference=inference)


class Sequential(LayerContainer):
    """
    Layer container that encapsulates a simple linear pathway of layers.

    Arguments:
        layers (list): List of objects which can be either a list of layers
                       (including layer containers).
    """
    def __init__(self, layers, name=None):
        super(Sequential, self).__init__(name)

        self.layers = [l for l in flatten(layers)]
        self._layers = filter(lambda x: type(x) not in (BranchNode,), self.layers)
        root = self._layers[0]
        assert (root.owns_output or
                type(root) in [Dropout, DataTransform]), "Sequential root must own outputs"

    def configure(self, in_obj):
        """
        Must receive a list of shapes for configuration (one for each pathway)
        the shapes correspond to the layer_container attribute

        Arguments:
            in_obj: any object that has an out_shape (Layer) or shape (Tensor, dataset)
        """
        config_layers = self.layers if in_obj else self._layers
        in_obj = in_obj if in_obj else self.layers[0]
        super(Sequential, self).configure(in_obj)
        prev_layer = None
        for l in config_layers:
            in_obj = l.configure(in_obj)
            if prev_layer is not None:
                prev_layer.set_next(l)
            prev_layer = l
        self.out_shape = in_obj.out_shape
        return self

    def allocate(self, shared_outputs=None):
        # get the layers that own their outputs
        alloc_layers = [l for l in self.layers if l.owns_output]
        alloc_layers[-1].allocate(shared_outputs)
        for l in self.layers:
            l.allocate()

    def allocate_deltas(self, global_deltas=None):
        def needs_extra_delta(ll):
            return True if type(ll) in (MergeBroadcast, ResidualModule) else False

        if not global_deltas:
            # See if we have any inception-ish layers:
            ndelta_bufs = 4 if any([needs_extra_delta(l) for l in self.layers]) else 2
            in_sizes = [np.prod(l.in_shape) for l in self.layers[1:]]
            if in_sizes:
                self.global_deltas = [self.be.iobuf(
                    max(in_sizes), parallelism="Data") for _ in range(ndelta_bufs)]
            else:
                self.global_deltas = None
        else:
            self.global_deltas = global_deltas

        for l in self.layers:
            l.set_deltas(self.global_deltas)

    def fprop(self, inputs, inference=False):
        x = inputs
        for l in self.layers:
            x = l.fprop(x, inference)
        return x

    def bprop(self, error, alpha=1.0, beta=0.0):
        for l in reversed(self._layers):
            if type(l.prev_layer) is BranchNode or l is self._layers[0]:
                error = l.bprop(error, alpha, beta)
            else:
                error = l.bprop(error)
        return self._layers[0].deltas

    def get_terminal(self):
        terminal = self.layers[-1].get_terminal()
        return terminal


class ResidualModule(LayerContainer):
    """
    Layer that encapsulates a sequential plus a residual skip branch, optionally
    containing a projection

    Arguments:
        layers (list): List of objects which can be either a list of layers (including layer
                       containers).
        projection (Initializer, optional): If a valid Initializer is supplied, then the skip
                                            layer will perform a 1x1 convolution with
                                            appropriate striding to match the size and shape
                                            of the output of the main branch.  The default is
                                            None, which means that the input to the module will
                                            be added directly to the output of the main branch
                                            with no projection applied.  NB:  IdentityInit are
                                            treated differently from regular Initializers in
                                            that the projection is applied, but those Identity
                                            weights are never updated.

    """
    def __init__(self, layers, projection=None, name='residual'):
        super(ResidualModule, self).__init__(name)

        self.layers = [Sequential([l for l in flatten(layers)])]
        convlayers = [l for l in self.layers[0].layers if type(l) is Convolution]
        nofm = convlayers[-1].convparams['K']
        skip_stride = convlayers[-2].convparams['str_h']

        self.owns_output = True
        self.error_views = None
        if projection is not None:
            self.skip_layer = Convolution((1, 1, nofm), init=projection, strides=skip_stride)
            if projection.name != "Identity":
                self.layers.append(self.skip_layer)
        else:
            self.skip_layer = None

    def configure(self, in_obj):
        """
        sets shape based parameters of this layer given an input tuple or int
        or input layer

        Arguments:
            in_obj (int, tuple, Layer or Tensor or dataset): object that provides shape
                                                             information for layer

        Returns:
            (tuple): shape of output data
        """
        super(ResidualModule, self).configure(in_obj)
        self.layers[0].configure(in_obj)
        self.out_shape = self.layers[0].out_shape
        if self.skip_layer is not None:
            self.skip_layer.configure(in_obj)
        return self

    def allocate(self, shared_outputs=None):
        self.outputs = self.be.iobuf(self.out_shape, shared=shared_outputs)
        self.layers[0].allocate(self.outputs)
        if self.skip_layer is not None:
            self.skip_layer.allocate(self.outputs)

    def set_deltas(self, delta_buffers):
        assert len(delta_buffers) == 4, "Need extra delta buffer pool for residual layers"
        self.layers[0].allocate_deltas(delta_buffers[1:3])
        self.layers[0].layers[0].set_deltas(delta_buffers[0:1])
        if self.skip_layer is not None:
            self.skip_layer.set_deltas(delta_buffers[0:1])
        self.deltas = self.be.iobuf(self.in_shape, shared=delta_buffers[0])
        delta_buffers.reverse()

    def fprop(self, inputs, inference=False):
        self.inputs = inputs
        self.layers[0].fprop(inputs, inference)
        if self.skip_layer is not None:
            self.skip_layer.fprop(inputs, inference, beta=1.0)
        else:
            self.outputs[:] = self.outputs + inputs
        return self.outputs

    def bprop(self, error, alpha=1.0, beta=0.0):
        if self.skip_layer is not None:
            self.skip_layer.bprop(error, alpha=alpha)
        else:
            self.deltas[:] = error
        self.layers[0].bprop(error, alpha=alpha, beta=1.0)
        return self.deltas

    def get_terminal(self):
        return self.layers[0].get_terminal()


class Tree(LayerContainer):
    """
    Layer container that encapsulates a simple linear pathway of layers.

    Arguments:
        layers (list): List of Sequential containers corresponding to the branches of the Tree.
                       The branches must be provided with main trunk first, and then the auxiliary
                       branches in the order the branch nodes are encountered
        name (string, optional): Name for the container
        alphas (list(float), optional): list of weighting factors to apply to each branch for
                                        backpropagating error.
    """

    def __init__(self, layers, name=None, alphas=None):
        super(Tree, self).__init__(name=name)
        self.layers = []
        for l in layers:
            if isinstance(l, Sequential):
                self.layers.append(l)
            elif isinstance(l, list):
                self.layers.append(Sequential(l))
            elif isinstance(l, Layer):
                self.layers.append(Sequential([l]))
            else:
                ValueError("Incompatible element for Tree container")

        self.alphas = [1.0 for _ in self.layers] if alphas is None else alphas

        # alphas and betas are used for back propagation
        # We want to ensure that the branches are ordered according to the origin of their roots
        # then the betas will be 0 for the last appearance of the root, and 1 for the rest,
        # but the trunk will always be 1 (since it contains all of the branch nodes)
        self.betas = []
        next_root = None
        for l in reversed(self.layers):
            root = l.layers[0]
            beta = 1.0 if (root is next_root or type(root) is not BranchNode) else 0.0
            next_root = root
            self.betas.append(beta)
        self.betas.reverse()

    def nested_str(self, level=0):
        ss = self.classnm + '\n'
        ss += '\n'.join([l.nested_str(level+1) for l in self.layers])
        return ss

    def configure(self, in_obj):
        super(Tree, self).configure(in_obj)
        self.layers[0].configure(in_obj)

        for l in self.layers[1:]:
            l.configure(None)
        self.out_shape = [l.out_shape for l in self.layers]
        return self

    def allocate(self, shared_outputs=None):
        for l in self.layers:
            l.allocate()
        self.outputs = [l.outputs for l in self.layers]

    def allocate_deltas(self, global_deltas=None):
        for l in reversed(self.layers):
            l.allocate_deltas()

    def fprop(self, inputs, inference=False):
        x = self.layers[0].fprop(inputs, inference)
        if inference:
            return x
        else:
            out = [x] + [l.fprop(None) for l in self.layers[1:]]
            return out

    def bprop(self, error):
        for l, e, a, b in reversed(zip(self.layers, error, self.alphas, self.betas)):
            l.bprop(e, alpha=a, beta=b)

    def get_terminal(self):
        return [l.get_terminal() for l in self.layers]


class MergeBroadcast(LayerContainer):
    """
    Branches a single incoming layer or object (broadcast) into multiple output paths that are
    then combined again (merged)

    Arguments:
        layers (list(list(Layer), LayerContainer): list of either layer lists,
                                                   or layer containers.  Elements that are
                                                   lists will be wrapped in Sequential
                                                   containers
        alphas (list(float), optional):  list of alpha values by which to weight the
                                         backpropagated errors
        name (str): Container name.  Defaults to "MergeBroadcast"
    """
    def __init__(self, layers, merge, alphas=None, name=None):
        """
        TODO add DOCSTRING
        """
        super(MergeBroadcast, self).__init__(name)

        # Input list of layers converts:
        #   lists to Sequential container
        #   singleton layers to Sequential containers of 1
        #   leaves Sequentials alone
        self.layers = []
        for l in layers:
            if isinstance(l, Sequential):
                self.layers.append(l)
            elif isinstance(l, list):
                self.layers.append(Sequential(l))
            elif isinstance(l, Layer):
                self.layers.append(Sequential([l]))
            else:
                ValueError("Incompatible element for MergeBroadcast Layer")
        self.betas = [1.0 for _ in self.layers]
        self.betas[-1] = 0.0
        self.alphas = [1.0 for _ in self.layers] if alphas is None else alphas

        self.merge = merge  # How this MergeBroadcast gets merged
        assert self.merge in ("recurrent", "depth", "stack")
        self.error_views = None
        self.owns_output = True
        self.outputs = None

    def __str__(self):
        ss = '\n\t'.join([str(l) for l in self.layers])
        ss = '\t' + self.classnm + '\n\t' + ss
        return ss

    def get_partitions(self, x, slices):
        """
        given a partitioning, slices, of an activation buffer, x, determine which axis to slice
        along depending on whether x is a sequential tensor or not
        """
        if x.shape[-1] != self.be.bsz:  # This is the sequential case
            return [x[:, sl] for sl in slices]
        else:
            return [x[sl] for sl in slices]

    def configure(self, in_obj):
        """
        sets shape based parameters of this layer given an input tuple or int
        or input layer

        Arguments:
            in_obj (int, tuple, Layer or Tensor or dataset): object that provides shape
                                                             information for layer

        Returns:
            (tuple): shape of output data
        """
        super(MergeBroadcast, self).configure(in_obj)

        # Receiving from single source -- distribute to branches
        for l in self.layers:
            l.configure(in_obj)
        self._configure_merge()
        return self

    def allocate(self, shared_outputs=None):
        if self.outputs is None:
            self.outputs = self.be.iobuf(self.out_shape, shared=shared_outputs)
        self.output_views = self.get_partitions(self.outputs, self.slices)
        for l, out_view in zip(self.layers, self.output_views):
            l.allocate(shared_outputs=out_view)

    def set_deltas(self, delta_buffers):
        assert len(delta_buffers) == 4, "Need extra delta buffer pool for merge broadcast layers"
        for l in self.layers:
            l.allocate_deltas(delta_buffers[1:3])
            l.layers[0].set_deltas(delta_buffers[0:1])

        # Special case if originating from a branch node
        if type(self.prev_layer) is BranchNode:
            self.deltas = self.be.iobuf(self.in_shape, shared=self.prev_layer.deltas)
        else:
            self.deltas = self.be.iobuf(self.in_shape, shared=delta_buffers[0])
            delta_buffers.reverse()

    def _configure_merge(self):
        """
        Helper function for configuring shapes depending on the merge concatenation type
        """
        in_shapes = [l.out_shape for l in self.layers]
        # Figure out how to merge
        if self.merge == "recurrent":
            catdims = [xs[1] for xs in in_shapes]
            self.out_shape = (in_shapes[0][0], sum(catdims))
            stride_size = self.be.bsz
        elif self.merge == "depth":
            catdims = [xs[0] for xs in in_shapes]
            self.out_shape = (sum(catdims),) + in_shapes[0][1:]
            stride_size = np.prod(in_shapes[0][1:])
        elif self.merge == "stack":
            catdims = [xs if isinstance(xs, int) else np.prod(xs) for xs in in_shapes]
            self.out_shape = sum(catdims)
            stride_size = 1
        end_idx = [idx * stride_size for idx in np.cumsum(catdims)]
        start_idx = [0] + end_idx[:-1]
        self.slices = [slice(s, e) for s, e in zip(start_idx, end_idx)]

    def fprop(self, inputs, inference=False):
        for l in self.layers:
            l.fprop(inputs, inference)
        return self.outputs

    def bprop(self, error, alpha=1.0, beta=0.0):
        self.betas[-1] = beta
        if self.error_views is None:
            self.error_views = self.get_partitions(error, self.slices)
        for l, e, a, b in reversed(zip(self.layers, self.error_views, self.alphas, self.betas)):
            l.bprop(e, alpha=a*alpha, beta=b)
        return self.deltas

    def get_terminal(self):
        terminals = [l.get_terminal() for l in self.layers]
        return terminals


class MergeMultistream(MergeBroadcast):
    """
    Merging multiple input sources via concatenation.  This container is similar to MergeBroadcast
    except that it receives different streams of input directly from a dataset.
    """
    def __init__(self, layers, merge, name=None):
        super(MergeMultistream, self).__init__(layers, merge=merge, name=name)

    def configure(self, in_obj):
        """
        Must receive a list of shapes for configuration (one for each pathway)
        the shapes correspond to the layer_container attribute

        Arguments:
            in_obj (list(Tensor)): list of Data tensors provided to each sequential container
        """
        self.prev_layer = None
        if not isinstance(in_obj, list):
            assert hasattr(in_obj, 'shape') and isinstance(in_obj.shape, list)
            in_obj = in_obj.shape
        assert isinstance(in_obj, list), "Multistream inputs must be interpretable as shapes"
        for inp, l in zip(in_obj, self.layers):
            l.configure(inp)
        self._configure_merge()
        return self

    def set_deltas(self, delta_buffers):
        for l in self.layers:
            l.allocate_deltas()

    def fprop(self, inputs, inference=False):
        for l, inp in zip(self.layers, inputs):
            l.fprop(inp, inference)
        return self.outputs

    def bprop(self, error, alpha=1.0, beta=0.0):
        if self.error_views is None:
            self.error_views = self.get_partitions(error, self.slices)
        for l, e in zip(self.layers, self.error_views):
            l.bprop(e)


class Multicost(NervanaObject):
    """
    Class used to compute cost from a Tree container with multiple outputs.
    The number of costs must match the number of outputs.  Costs will be applied to the outputs
    in the same order that they occur in the Tree.

    The targets used for the cost can either be provided from the dataset as a list or tuple,
    one for each cost, or, if only a single target is provided, the same target is used for all
    costs.  This is useful for providing multiple cost branches computing the same error at
    different stages of the network as in GoogLeNet.
    """

    def __init__(self, costs, weights=None, name=None):
        super(Multicost, self).__init__(name)
        self.costs = costs
        self.weights = [1.0 for c in costs] if weights is None else weights
        self.errors = None
        self.inputs = None
        self.costfunc = costs[0].costfunc  # For displaying during callbacks

    @classmethod
    def gen_class(cls, pdict):
        costs = []
        for cost in pdict['costs']:
            typ = cost['type']
            if typ.find('neon.') == -1:
                typ = 'neon.layers.layer.' + typ
            ccls = load_class(typ)
            costs.append(ccls.gen_class(cost['config']))
        pdict['costs'] = costs
        return cls(**pdict)

    def initialize(self, in_obj):
        assert hasattr(in_obj, 'layers'), "MultiCost must be passed a layer container"
        terminals = in_obj.get_terminal()
        for c, ll in zip(self.costs, terminals):
            c.initialize(ll)

    @property
    def cost(self):
        return self.costs[0].cost

    @property
    def outputs(self):
        return self.costs[0].outputs

    def get_description(self, **kwargs):
        desc = super(Multicost, self).get_description()
        costs = desc['config'].pop('costs')
        desc['config']['costs'] = []
        for cost in costs:
            desc['config']['costs'].append(cost.get_description())
        self._desc = desc
        return desc

    def get_cost(self, inputs, targets):
        """
        Compute the cost function over a list of inputs and targets.

        Arguments:
            inputs (list(Tensor)): list of Tensors containing input values to be compared to
                                   targets
            targets (Tensor, list(Tensor)): either a list of Tensors containing target values, or
                                            a single target Tensor that will be mapped to each
                                            input

        Returns:
            Tensor containing cost
        """
        if not isinstance(inputs, list):
            return self.costs[0].get_cost(inputs, targets)
        else:
            ltargets = targets if type(targets) in (tuple, list) else [targets for c in self.costs]
            costvals = [c.get_cost(i, t) for c, i, t in zip(self.costs, inputs, ltargets)]
            sum_optree = reduce(add, [w * c for w, c in zip(self.weights, costvals)])
            costvals[0][:] = sum_optree
            return costvals[0]

    def get_errors(self, inputs, targets):
        """
        Get a list of errors for backpropagating to a Tree container that has multiple output
        nodes.

        Arguments:
            inputs (list(Tensor)): list of Tensors containing input values to be compared to
                                   targets
            targets (Tensor, list(Tensor)): either a list of Tensors containing target values, or
                                            a single target Tensor that will be mapped to each
                                            input
        Returns:
            list of Tensors containing errors for each input
        """

        l_targets = targets if type(targets) in (tuple, list) else [targets for c in self.costs]
        if self.errors is None:
            self.errors = [c.deltas for c in self.costs]

        for c, i, t in zip(self.costs, inputs, l_targets):
            c.get_errors(i, t)

        return self.errors
