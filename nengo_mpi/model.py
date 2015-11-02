"""MPIModel"""

try:
    from mpi_sim import MpiSimulator
    mpi_sim_available = True
except ImportError:
    print (
        "mpi_sim.so not available. Network files may be created, "
        "but simulations cannot be run.")
    mpi_sim_available = False

try:
    import h5py as h5
    h5py_available = True
except ImportError:
    print (
        "h5py not available. nengo_mpi cannot be used.")
    h5py_available = False


import nengo
from nengo import builder
from nengo.builder import Builder as DefaultBuilder
from nengo.neurons import LIF, LIFRate, RectifiedLinear, Sigmoid
from nengo.neurons import AdaptiveLIF, AdaptiveLIFRate, Izhikevich
from nengo.synapses import LinearFilter, Triangle
from nengo.processes import WhiteNoise, FilteredNoise, BrownNoise, WhiteSignal
from nengo.utils.graphs import toposort
from nengo.utils.builder import full_transform
from nengo.utils.simulator import operator_depencency_graph
from nengo.cache import NoDecoderCache
from nengo.network import Network
from nengo.connection import Connection
from nengo.ensemble import Ensemble
from nengo.node import Node
from nengo.probe import Probe

from nengo_mpi.spaun_mpi import SpaunStimulus, build_spaun_stimulus
from nengo_mpi.spaun_mpi import SpaunStimulusOperator

import numpy as np
from collections import defaultdict, OrderedDict
import warnings
from itertools import chain
import re
import os
import tempfile

import logging
logger = logging.getLogger(__name__)

OP_DELIM = ";"
SIGNAL_DELIM = ":"
PROBE_DELIM = "|"


def make_builder(base_function):
    """ Return an augmented version of an existing builder function.

    The only difference between `base_function` and the returned function is
    that it assumes the model is an instance of `MpiModel`, and uses that model
    to record which ops are created as part of building which high-level
    objects.

    """

    def build_object(model, obj, *args, **kwargs):
        try:
            model.push_object(obj)
        except AttributeError:
            raise ValueError(
                "Must use an instance of MpiModel.")

        r = base_function(model, obj, *args, **kwargs)
        model.pop_object()
        return r

    build_object.__doc__ = (
        "Builder function augmented to make use "
        "of MpiModels.\n\n" + str(base_function.__doc__))

    return build_object


class MpiBuilder(DefaultBuilder):
    builders = {}

MpiBuilder.builders.update(DefaultBuilder.builders)

with warnings.catch_warnings():

    # Ignore the warning generated by overwriting the builder functions.
    warnings.simplefilter('ignore')

    MpiBuilder.register(Ensemble)(
        make_builder(builder.build_ensemble))

    MpiBuilder.register(Node)(
        make_builder(builder.build_node))

    MpiBuilder.register(Connection)(
        make_builder(builder.build_connection))

    MpiBuilder.register(Probe)(
        make_builder(builder.build_probe))

    MpiBuilder.register(SpaunStimulus)(
        make_builder(build_spaun_stimulus))

    def mpi_build_network(model, network):
        """ Build a nengo Network for nengo_mpi.

        This function replaces nengo.builder.build_network.

        For each connection that emenates from a Node, has a non-None
        pre-slice, AND has no function attached to it, we replace it
        with a Connection that is functionally equivalent, but has
        the slicing moved into the transform. This is done because
        in some such cases, the refimpl nengo builder will implement the
        slicing using a python function, which we want to avoid in nengo_mpi.

        Parameters
        ----------
        model: MpiModel
            The model to which created components will be added.
        network: nengo.Network
            The network to be built.

        """
        remove_conns = []

        for conn in network.connections:
            replace_connection = (
                isinstance(conn.pre_obj, Node)
                and conn.pre_slice != slice(None)
                and conn.function is None)

            if replace_connection:
                transform = full_transform(conn)

                with network:
                    Connection(
                        conn.pre_obj, conn.post_obj,
                        synapse=conn.synapse,
                        transform=transform, solver=conn.solver,
                        learning_rule_type=conn.learning_rule_type,
                        eval_points=conn.eval_points,
                        scale_eval_points=conn.scale_eval_points,
                        seed=conn.seed)

                remove_conns.append(conn)

        if remove_conns:
            network.objects[Connection] = filter(
                lambda c: c not in remove_conns, network.connections)

            network.connections = network.objects[Connection]

        return builder.build_network(model, network)

    MpiBuilder.register(Network)(
        make_builder(mpi_build_network))


def pyfunc_checks(val):
    """Check a value to make sure it conforms to expectations.

    If the output can possibly be treated as a scalar, convert it
    to a python float. Otherwise, convert it to a numpy ndarray.

    Parameters
    ----------
    val: any
        Value to be checked.

    """
    if isinstance(val, list):
        val = np.array(val, dtype=np.float64)

    elif isinstance(val, int):
        val = float(val)

    elif isinstance(val, float):
        if isinstance(val, np.float64):
            val = float(val)

    elif not isinstance(val, np.ndarray):
        raise ValueError(
            "python function returning unexpected value, %s" % str(val))

    if isinstance(val, np.ndarray):
        val = np.squeeze(val)

        if val.size == 1:
            val = float(val)
        elif getattr(val, 'dtype', None) != np.float64:
            val = np.asarray(val, dtype=np.float64)

    return val


def make_checked_func(func, t_in, takes_input):
    """Create checked version of an existing function.

    Returns a version of `func' whose output is first checked to make
    sure that it conforms to expectations.

    Parameters
    ----------
    func: function
        Function to create checked version of.
    t_in: bool
        Whether the function takes the current time step as an argument.
    takes_input: bool
        Whether the function takes an input other than the current time step.
    """

    def f():
        return pyfunc_checks(func())

    def ft(t):
        return pyfunc_checks(func(t))

    def fit(t, i):
        return pyfunc_checks(func(t, i))

    if t_in and takes_input:
        return fit
    elif t_in or takes_input:
        return ft
    else:
        return f


class MpiSend(builder.operator.Operator):
    """ Operator that sends a Signal to a different process.

    Stores the signal that the operator will send and the process
    that it will be sent to. No `makestep` is defined, as it will
    never be called (this operator is never used in python simulations).

    """

    def __init__(self, dst, tag, signal):
        self.sets = []
        self.incs = []
        self.reads = []
        self.updates = []

        self.dst = dst
        self.tag = tag
        self.signal = signal


class MpiRecv(builder.operator.Operator):
    """ Operator that receives a signal from another process.

    Stores the signal that the operator will receive and the process
    that it will be received from. No `makestep` is defined, as it will
    never be called (this operator is never used in python simulations).

    """

    def __init__(self, src, tag, signal):
        self.sets = []
        self.incs = []
        self.reads = []
        self.updates = []

        self.src = src
        self.tag = tag
        self.signal = signal


def split_connection(conn_ops, signal):
    """ Split up the operators implementing a Connection.

    Split the operators belonging to a connection into a
    ``pre'' group and a ``post'' group. The connection is assumed
    to contain exactly 1 operation performing an update, which
    is assigned to the pre group. All ops that write to signals
    which are read by this updating op are assumed to belong to
    the pre group (as are all ops that write to signals which
    *those* ops read from, etc.). The remaining ops are assigned
    to the post group.

    Parameters
    ----------
    conn_ops: list
        List of operators implementing a Connection.
    signal: Signal
        The signal where the connection will be split. Must be a
        signal that is updated by one of the operators in `conn_ops`.

    Returns
    -------
    pre_ops: A list of the ops that come before the updated signal.
    post_ops: A list of the ops that come after the updated signal.

    """
    pre_ops = []

    for op in conn_ops:
        if signal in op.updates:
            pre_ops.append(op)

    assert len(pre_ops) == 1

    reads = pre_ops[0].reads

    post_ops = filter(
        lambda op: op not in pre_ops, conn_ops)

    changed = True
    while changed:
        changed = []

        for op in post_ops:
            writes = set(op.incs) | set(op.sets)

            if writes & set(reads):
                pre_ops.append(op)
                reads.extend(op.reads)
                changed.append(op)

        post_ops = filter(
            lambda op: op not in changed, post_ops)

    return pre_ops, post_ops


def make_key(obj):
    """ Create a unique key for an object.

    Must reproducable (i.e. produce the same key if called with
    the same object multiple times).

    """
    if isinstance(obj, builder.signal.SignalView):
        return id(obj.base)
    else:
        return id(obj)


def signal_to_string(signal, delim=SIGNAL_DELIM):
    """ Convert a signal to a string.

    The format of the returned string is:
        signal_key:shape:elemstrides:offset

    """
    shape = signal.shape if signal.shape else 1
    strides = signal.elemstrides if signal.elemstrides else 1

    signal_args = [
        make_key(signal), shape, strides, signal.offset]

    signal_string = delim.join(map(str, signal_args))
    signal_string = signal_string.replace(" ", "")
    signal_string = signal_string.replace("(", "")
    signal_string = signal_string.replace(")", "")

    return signal_string


def ndarray_to_string(a):
    s = "%d,%d," % np.atleast_2d(a).shape
    s += ",".join([str(n) for n in a.flatten()])
    return s


def store_string_list(
        h5_file, dset_name, strings, final_null=True, compression='gzip'):
    """Store a list of strings in a dataset in an hdf5 file or group.

    In the created dataset, the strings in `strings` are separated by null
    characters. An additional null character can optionally being
    added at the end.

    """
    big_string = '\0'.join(strings)

    if final_null:
        big_string += '\0'

    data = np.array(list(big_string))
    dset = h5_file.create_dataset(
        dset_name, data=data, dtype='S1', compression=compression)

    dset.attrs['n_strings'] = len(strings)


# Stole this from nengo_ocl
def get_closures(f):
    return OrderedDict(zip(
        f.__code__.co_freevars, (c.cell_contents for c in f.__closure__)))


class MpiModel(builder.Model):
    """Output of the MpiBuilder, used by nengo_mpi.Simulator.

    MpiModel differs from the Model in the reference implementation in that
    as the model is built, MpiModel keeps track of the high-level nengo object
    (e.g. Ensemble) currently being built. This permits it to track which
    operators implement which high-level objects, so that those operators can
    later be added to the correct MPI component (required since MPI components
    are specified in terms of the high-level objects).

    Parameters
    ----------
    n_components: int
        Number of components that the network will be divided into.
    assignments:
        A dictionary mapping from high-level objects to component
        indices (ints).  All high-level objects in the Network must appear as
        keys in this dictionary.
    dt: float
        Step length.
    decoder_cache: DecoderCache
        DecoderCache object to use.
    save_file: string
        Name of file to save the built network to. If a non-empty string is
        provided, then instead of creating a runnable simulator, the result
        of building the model is saved to a file which can be loaded by
        the executables bin/nengo_mpi and bin/nengo_cpp to run simulations.

    """
    def __init__(
            self, n_components, assignments, dt=0.001, label=None,
            decoder_cache=NoDecoderCache(), save_file=""):

        if not h5py_available:
            raise Exception("h5py not available.")

        self.n_components = n_components
        self.assignments = assignments

        if not save_file and not mpi_sim_available:
            raise ValueError(
                "mpi_sim.so is unavailable, so nengo_mpi can only save "
                "network files (cannot run simulations). However, save_file "
                "argument was empty.")

        # Only create a working simulator if our goal is not to simply
        # save the network to a file
        self.mpi_sim = MpiSimulator() if not save_file else None

        self.h5_compression = 'gzip'
        self.op_strings = defaultdict(list)
        self.probe_strings = defaultdict(list)
        self.all_probe_strings = []

        if not save_file:
            save_file = tempfile.mktemp()

        self.save_file_name = save_file

        # for each component, stores the keys of the signals that have
        # to be sent and received, respectively
        self.send_signals = defaultdict(list)
        self.recv_signals = defaultdict(list)

        # for each component, stores the signals that have
        # already been added to that component.
        self.signals = defaultdict(list)
        self.signal_key_set = defaultdict(set)
        self.total_signal_size = defaultdict(int)

        # component index (int) -> list of operators
        # stores the operators for each component
        self.component_ops = defaultdict(list)

        # probe -> C++ key (long int)
        # Used to query the C++ simulator for probe data
        self.probe_keys = {}

        self._object_context = [None]

        # high-level nengo object -> list of operators
        # stores the operators implementing each high-level object
        self.object_ops = defaultdict(list)

        self._mpi_tag = 0

        self.pyfunc_args = []

        super(MpiModel, self).__init__(dt, label, decoder_cache)

    @property
    def runnable(self):
        """ Return whether this MpiModel can immediately run a simulation.

        If save_file was not an empty string when __init__ was called, then
        this should always return False. Otherwise, returns True iff
        self.finalize_build has been called and completed successfully.

        """
        return self.mpi_sim is not None

    def __str__(self):
        return "MpiModel: %s" % self.label

    def sanitize(self, s):
        s = re.sub('([0-9])L', lambda x: x.groups()[0], s)
        return s

    def build(self, obj, *args, **kwargs):
        """ Overrides Model.build """
        return MpiBuilder.build(self, obj, *args, **kwargs)

    def _next_mpi_tag(self):
        """ Return the next mpi tag.

        Used to ensure that each Connection which straddles a component
        boundary uses a unique tag.

        """
        mpi_tag = self._mpi_tag
        self._mpi_tag += 1
        return mpi_tag

    def push_object(self, obj):
        """ Push high-level object onto context stack.

        So that we can record which operators implement the object.

        """
        self._object_context.append(obj)

    def pop_object(self):
        """ Pop high-level object off of context stack.

        Once an object has been popped from the context stack, we know that
        it has finished building, and we can add the operators that implement
        the object to the MpiModel. We add the operators for the object to
        the component that the object is assigned to (which is stored in the
        self.assignments dictionary).

        The only exceptions are Connections; when we pop a Connection whose
        pre object and post object are on different components, then some of
        operators implementing the connection go to one component, and there
        rest go to another.

        """
        obj = self._object_context.pop()

        if not isinstance(obj, Connection):
            component = self.assignments[obj]

            self.assign_ops(component, self.object_ops[obj])

        else:
            conn = obj
            pre_component = self.assignments[conn.pre_obj]
            post_component = self.assignments[conn.post_obj]

            if pre_component == post_component:
                self.assign_ops(pre_component, self.object_ops[conn])

            else:
                if conn.learning_rule_type:
                    raise Exception(
                        "Connections crossing component boundaries "
                        "must not have learning rules.")

                try:
                    synapse_op = (
                        op for op in self.object_ops[conn]
                        if isinstance(op, builder.synapses.SimSynapse)).next()
                except StopIteration:
                    raise Exception(
                        "Connections crossing component boundaries "
                        "must be have synapses so that there is an update.")

                signal = synapse_op.output

                tag = self._next_mpi_tag()

                self.send_signals[pre_component].append(
                    (signal, tag, post_component))
                self.recv_signals[post_component].append(
                    (signal, tag, pre_component))

                pre_ops, post_ops = split_connection(
                    self.object_ops[conn], signal)

                self.assign_ops(pre_component, pre_ops)
                self.assign_ops(post_component, post_ops)

    def assign_ops(self, component, ops):
        """ Assign a sequence of operators to a component.

        Parameters
        ----------
        component: int
            Component to add the operators to.
        ops: list of nengo.builder.operator.Operator instances
            Operators to add the model.

        """
        for op in ops:
            for signal in op.all_signals:
                key = make_key(signal)

                if key not in self.signal_key_set[component]:
                    logger.debug(
                        "Component %d: Adding signal %s with key: %s",
                        component, signal, make_key(signal))

                    self.signal_key_set[component].add(key)
                    self.signals[component].append((key, signal))
                    self.total_signal_size[component] += signal.size

        self.component_ops[component].extend(ops)

    def add_op(self, op):
        """ Add operator to model. Overrides Model.add_op.

        Records that the operator was added as part of building
        the object that is on top of the _object_context stack.

        """
        self.object_ops[self._object_context[-1]].append(op)

    def finalize_build(self):
        """ Finalize the build step.

        Called once the MpiBuilder has finished running. Finalizes
        operators and probes, converting them to strings. Then writes
        all relevant information (signals, ops and probes for each component)
        to an HDF5 file. Then, if self.mpi_sim is not None (so we want to
        create a runnable MPI simulator), calls self.mpi_sim.load_file, which
        tells the C++ code to load the HDF5 file we have just written and
        create a working simulator.

        """
        all_ops = list(chain(
            *[self.component_ops[component]
              for component in range(self.n_components)]))

        dg = operator_depencency_graph(all_ops)
        global_ordering = [
            op for op in toposort(dg) if hasattr(op, 'make_step')]
        self.global_ordering = {op: i for i, op in enumerate(global_ordering)}

        self._finalize_ops()
        self._finalize_probes()

        with h5.File(self.save_file_name, 'w') as save_file:
            save_file.attrs['dt'] = self.dt
            save_file.attrs['n_components'] = self.n_components

            for component in range(self.n_components):
                component_group = save_file.create_group(str(component))

                # signals
                signals = self.signals[component]
                signal_dset = component_group.create_dataset(
                    'signals', (self.total_signal_size[component],),
                    dtype='float64', compression=self.h5_compression)

                offset = 0
                for key, sig in signals:
                    A = sig.base._value

                    if A.ndim == 0:
                        A = np.reshape(A, (1, 1))

                    if A.dtype != np.float64:
                        A = A.astype(np.float64)

                    signal_dset[offset:offset+A.size] = A.flatten()
                    offset += A.size

                # signal keys
                component_group.create_dataset(
                    'signal_keys', data=[long(key) for key, sig in signals],
                    dtype='int64', compression=self.h5_compression)

                # signal shapes
                def pad(x):
                    return (
                        (1, 1) if len(x) == 0 else (
                            (x[0], 1) if len(x) == 1 else x))

                component_group.create_dataset(
                    'signal_shapes',
                    data=np.array([pad(sig.shape) for key, sig in signals]),
                    dtype='u2', compression=self.h5_compression)

                # signal_labels
                signal_labels = [str(p[1]) for p in signals]
                store_string_list(
                    component_group, 'signal_labels', signal_labels,
                    compression=self.h5_compression)

                # operators
                op_strings = self.op_strings[component]
                store_string_list(
                    component_group, 'operators', op_strings,
                    compression=self.h5_compression)

                # probes
                probe_strings = self.probe_strings[component]
                store_string_list(
                    component_group, 'probes', probe_strings,
                    compression=self.h5_compression)

            probe_strings = self.probe_strings[component]
            store_string_list(
                save_file, 'probe_info', self.all_probe_strings,
                compression=self.h5_compression)

        if self.mpi_sim is not None:
            self.mpi_sim.load_network(self.save_file_name)
            os.remove(self.save_file_name)

            for args in self.pyfunc_args:
                f = {
                    'N': self.mpi_sim.create_PyFunc,
                    'I': self.mpi_sim.create_PyFuncI,
                    'O': self.mpi_sim.create_PyFuncO,
                    'IO': self.mpi_sim.create_PyFuncIO}[args[0]]
                f(*args[1:])

            self.mpi_sim.finalize_build()

    def _finalize_ops(self):
        """ Finalize operators.

        Main jobs are to create MpiSend and MpiRecv opreators based on
        send_signals and recv_signals, and to turn all ops belonging to
        the `component` into strings, which are then stored in self.op_strings.
        PyFunc ops are the only exception, as it is not generally possible to
        encode an arbitrary python function as a string.

        """
        for component in range(self.n_components):
            send_signals = self.send_signals[component]
            recv_signals = self.recv_signals[component]
            component_ops = self.component_ops[component]

            for signal, tag, dst in send_signals:
                mpi_send = MpiSend(dst, tag, signal)

                update_indices = filter(
                    lambda i: signal in component_ops[i].updates,
                    range(len(component_ops)))

                assert len(update_indices) == 1

                self.global_ordering[mpi_send] = (
                    self.global_ordering[component_ops[update_indices[0]]]
                    + 0.5)

                # Put the send after the op that updates the signal.
                component_ops.insert(update_indices[0]+1, mpi_send)

            for signal, tag, src in recv_signals:
                mpi_recv = MpiRecv(src, tag, signal)

                read_indices = filter(
                    lambda i: signal in component_ops[i].reads,
                    range(len(component_ops)))

                self.global_ordering[mpi_recv] = (
                    self.global_ordering[component_ops[read_indices[0]]] - 0.5)

                # Put the recv in front of the first op that reads the signal.
                component_ops.insert(read_indices[0], mpi_recv)

            op_order = sorted(
                component_ops, key=self.global_ordering.__getitem__)

            for op in op_order:
                op_type = type(op)

                if op_type == builder.node.SimPyFunc:
                    if not self.runnable:
                        raise Exception(
                            "Cannot create SimPyFunc operator "
                            "when saving to file.")

                    t_in = op.t_in
                    fn = op.fn
                    x = op.x

                    if x is None:
                        if op.output is None:
                            pyfunc_args = ["N", fn, t_in]
                        else:
                            pyfunc_args = [
                                "O", make_checked_func(fn, t_in, False),
                                t_in, signal_to_string(op.output)]

                    else:
                        input_array = x.value

                        if op.output is None:
                            pyfunc_args = [
                                "I", fn, t_in,
                                signal_to_string(x), input_array]

                        else:
                            pyfunc_args = [
                                "IO", make_checked_func(fn, t_in, True), t_in,
                                signal_to_string(x), input_array,
                                signal_to_string(op.output)]

                    self.pyfunc_args.append(
                        pyfunc_args + [self.global_ordering[op]])
                else:
                    op_string = self._op_to_string(op)

                    if op_string:
                        logger.debug(
                            "Component %d: Adding operator with string: %s",
                            component, op_string)

                        self.op_strings[component].append(op_string)

    def _op_to_string(self, op):
        """ Convert operator into a string.

        Such strings will eventually be used to construct operators in the
        C++ code. See the MpiSimulatorChunk::add_op for details on how these
        strings are used by the C++ code.

        We prepend the operator's index in the global ordering of
        operators, which allows the C++ code to put the operators in
        the appropriate order.

        """
        op_type = type(op)

        if op_type == builder.operator.Reset:
            op_args = ["Reset", signal_to_string(op.dst), op.value]

        elif op_type == builder.operator.Copy:
            op_args = [
                "Copy", signal_to_string(op.dst), signal_to_string(op.src)]

        elif op_type == builder.operator.SlicedCopy:

            try:
                seq_A = list(iter(op.a_slice))
                start_A, stop_A, step_A = 0, 0, 0
            except:
                seq_A = []
                if op.a_slice == Ellipsis:
                    start_A, stop_A, step_A = 0, op.a.size, 1
                else:
                    start_A, stop_A, step_A = op.a_slice.indices(op.a.size)

            try:
                seq_B = list(iter(op.b_slice))
                start_B, stop_B, step_B = 0, 0, 0
            except:
                seq_B = []
                if op.b_slice == Ellipsis:
                    start_B, stop_B, step_B = 0, op.b.size, 1
                else:
                    start_B, stop_B, step_B = op.b_slice.indices(op.b.size)

            op_args = [
                "SlicedCopy", signal_to_string(op.b), signal_to_string(op.a),
                int(op.inc), start_A, stop_A, step_A, start_B, stop_B, step_B,
                str(seq_A), str(seq_B)]

        elif op_type == builder.operator.DotInc:
            op_args = [
                "DotInc", signal_to_string(op.A), signal_to_string(op.X),
                signal_to_string(op.Y)]

        elif op_type == builder.operator.ElementwiseInc:
            op_args = [
                "ElementwiseInc", signal_to_string(op.A),
                signal_to_string(op.X), signal_to_string(op.Y)]

        elif op_type == builder.neurons.SimNeurons:
            n_neurons = op.J.size
            neuron_type = type(op.neurons)

            if neuron_type is LIF:
                tau_ref = op.neurons.tau_ref
                tau_rc = op.neurons.tau_rc
                min_voltage = op.neurons.min_voltage

                voltage_signal = signal_to_string(op.states[0])
                ref_time_signal = signal_to_string(op.states[1])

                op_args = [
                    "LIF", n_neurons, tau_rc, tau_ref, min_voltage, self.dt,
                    signal_to_string(op.J), signal_to_string(op.output),
                    voltage_signal, ref_time_signal]

            elif neuron_type is LIFRate:
                tau_ref = op.neurons.tau_ref
                tau_rc = op.neurons.tau_rc
                op_args = [
                    "LIFRate", n_neurons, tau_rc, tau_ref,
                    signal_to_string(op.J), signal_to_string(op.output)]

            elif neuron_type is AdaptiveLIF:
                tau_n = op.neurons.tau_n
                inc_n = op.neurons.inc_n

                tau_rc = op.neurons.tau_rc
                tau_ref = op.neurons.tau_ref

                min_voltage = op.neurons.min_voltage

                voltage_signal = signal_to_string(op.states[0])
                ref_time_signal = signal_to_string(op.states[1])
                adaptation = signal_to_string(op.states[2])

                op_args = [
                    "AdaptiveLIF", n_neurons, tau_n, inc_n, tau_rc, tau_ref,
                    min_voltage, self.dt, signal_to_string(op.J),
                    signal_to_string(op.output), voltage_signal,
                    ref_time_signal, adaptation]

            elif neuron_type is AdaptiveLIFRate:
                tau_n = op.neurons.tau_n
                inc_n = op.neurons.inc_n

                tau_rc = op.neurons.tau_rc
                tau_ref = op.neurons.tau_ref

                adaptation = signal_to_string(op.states[0])

                op_args = [
                    "AdaptiveLIFRate", n_neurons, tau_n, inc_n,
                    tau_rc, tau_ref, self.dt, signal_to_string(op.J),
                    signal_to_string(op.output), adaptation]

            elif neuron_type is RectifiedLinear:
                op_args = [
                    "RectifiedLinear", n_neurons, signal_to_string(op.J),
                    signal_to_string(op.output)]

            elif neuron_type is Sigmoid:
                op_args = [
                    "Sigmoid", n_neurons, op.neurons.tau_ref,
                    signal_to_string(op.J), signal_to_string(op.output)]

            elif neuron_type is Izhikevich:
                tau_recovery = op.neurons.tau_recovery
                coupling = op.neurons.coupling
                reset_voltage = op.neurons.reset_voltage
                reset_recovery = op.neurons.reset_recovery

                voltage = signal_to_string(op.states[0])
                recovery = signal_to_string(op.states[1])

                op_args = [
                    "Izhikevich", n_neurons, tau_recovery, coupling,
                    reset_voltage, reset_recovery, self.dt,
                    signal_to_string(op.J), signal_to_string(op.output),
                    voltage, recovery]

            else:
                raise NotImplementedError(
                    'nengo_mpi cannot handle neurons of type ' +
                    str(neuron_type))

        elif op_type == builder.synapses.SimSynapse:

            if isinstance(op.synapse, LinearFilter):

                step = op.synapse.make_step(self.dt, [])
                den = step.den
                num = step.num

                if len(num) == 1 and len(den) == 0:
                    op_args = [
                        "NoDenSynapse", signal_to_string(op.input),
                        signal_to_string(op.output), num[0]]
                elif len(num) == 1 and len(den) == 1:
                    op_args = [
                        "SimpleSynapse", signal_to_string(op.input),
                        signal_to_string(op.output), den[0], num[0]]
                else:
                    op_args = [
                        "Synapse", signal_to_string(op.input),
                        signal_to_string(op.output),
                        ",".join(map(str, num)),
                        ",".join(map(str, den))]

            elif isinstance(op.synapse, Triangle):
                f = op.synapse.make_step(self.dt, op.output)
                closures = get_closures(f)
                n0 = closures['n0']
                ndiff = closures['ndiff']
                x = closures['x']
                n_taps = x.maxlen

                op_args = [
                    "TriangleSynapse", signal_to_string(op.input),
                    signal_to_string(op.output), n0, ndiff, n_taps]

            else:
                raise NotImplementedError(
                    'nengo_mpi cannot handle synapses of '
                    'type %s' % type(op.synapse))

        elif op_type == builder.processes.SimProcess:
            process_type = type(op.process)

            if process_type is WhiteNoise:
                assert type(op.process.dist) is nengo.dists.Gaussian
                mean = op.process.dist.mean
                std = op.process.dist.std
                do_scale = op.process.scale

                op_args = [
                    "WhiteNoise", signal_to_string(op.output),
                    float(mean), float(std), int(do_scale), int(op.inc),
                    self.dt]

            elif process_type is WhiteSignal:
                f = op.process.make_step(
                    0, op.output.size, self.dt, np.random.RandomState())
                closures = get_closures(f)
                assert closures['dt'] == self.dt
                coefs = closures['signal']

                op_args = [
                    "WhiteSignal", signal_to_string(op.output),
                    ndarray_to_string(coefs)]

            elif process_type in [FilteredNoise, BrownNoise]:
                raise NotImplementedError(
                    'nengo_mpi cannot handle processes of '
                    'type %s' % str(process_type))
            else:
                raise NotImplementedError(
                    'Unrecognized process type: %s.' % str(process_type))

        elif op_type == builder.operator.PreserveValue:
            logger.debug(
                "Skipping PreserveValue, operator: %s, signal: %s",
                str(op.dst), signal_to_string(op.dst))

            op_args = []

        elif op_type == MpiSend:
            signal_key = make_key(op.signal)
            op_args = ["MpiSend", op.dst, op.tag, signal_key]

        elif op_type == MpiRecv:
            signal_key = make_key(op.signal)
            op_args = ["MpiRecv", op.src, op.tag, signal_key]

        elif op_type == SpaunStimulusOperator:
            output = signal_to_string(op.output)

            op_args = [
                "SpaunStimulus", output, op.stimulus_sequence,
                op.present_interval, op.present_blanks, op.identifier]

        else:
            raise NotImplementedError(
                "nengo_mpi cannot handle operator of "
                "type %s" % str(op_type))

        if op_args:
            op_args = [self.global_ordering[op]] + op_args

        op_string = OP_DELIM.join(map(str, op_args))
        op_string = op_string.replace(" ", "")
        op_string = op_string.replace("(", "")
        op_string = op_string.replace(")", "")

        return op_string

    def _finalize_probes(self):
        """ Finalize probes.

        Main job is to convert all probes in self.probes into strings,
        which then get stored in self.probe_strings.

        """
        for probe in self.probes:
            period = (
                1 if probe.sample_every is None
                else probe.sample_every / self.dt)

            probe_key = make_key(probe)
            self.probe_keys[probe] = probe_key

            signal = self.sig[probe]['in']
            signal_string = signal_to_string(signal)

            component = self.assignments[probe]

            logger.debug(
                "Component: %d: Adding probe of signal %s.\n"
                "probe_key: %d, signal_string: %s, period: %d",
                component, str(signal), probe_key,
                signal_string, period)

            probe_string = PROBE_DELIM.join(
                str(i)
                for i
                in [component, probe_key, signal_string, period, str(probe)])

            self.probe_strings[component].append(probe_string)
            self.all_probe_strings.append(probe_string)
