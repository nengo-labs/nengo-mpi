"""MPIModel"""

from nengo import builder
from nengo.builder import Builder as DefaultBuilder
from nengo.neurons import LIF, LIFRate, RectifiedLinear, Sigmoid
from nengo.synapses import LinearFilter, Lowpass, Alpha
from nengo.utils.filter_design import cont2discrete
from nengo.utils.graphs import toposort
from nengo.utils.builder import full_transform
from nengo.utils.simulator import operator_depencency_graph
from nengo.cache import NoDecoderCache

from nengo.network import Network
from nengo.connection import Connection
from nengo.ensemble import Ensemble
from nengo.node import Node
from nengo.probe import Probe

from spaun_mpi import SpaunStimulus, build_spaun_stimulus
from spaun_mpi import SpaunStimulusOperator

import numpy as np
from collections import defaultdict
import warnings
from itertools import chain

import logging
logger = logging.getLogger(__name__)


def make_builder(base):
    """
    Create a version of an existing builder function whose only difference
    is that it assumes the model is an instance of MpiModel, and uses that
    model to record which ops are built as part of building which high-level
    objects.

    Parameters
    ----------
    base: The existing builder function that we want to augment.

    """

    def build_object(model, obj):
        try:
            model.push_object(obj)
        except AttributeError:
            raise ValueError(
                "Must use an instance of MpiModel.")

        r = base(model, obj)
        model.pop_object()
        return r

    build_object.__doc__ = (
        "Builder function augmented to make use "
        "of MpiModels.\n\n" + str(base.__doc__))

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
        """
        For each connection that emenates from a Node, has a non-None
        pre-slice, and has no function attached to it, we replace it
        with a connection that is functionally equivalent, but has
        the slicing moved into the transform. This is done because
        in some such cases, the nengo builder will implement the
        slicing using a pyfunc, which we want to avoid in nengo_mpi.
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
                        modulatory=conn.modulatory,
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


class DummyNdarray(object):
    """
    A dummy array intended to act as a place holder for an
    ndarray. Preserves the type, shape, stride and size
    attributes of the original ndarray, but not its conents.
    """

    def __init__(self, value):
        self.dtype = value.dtype
        self.shape = value.shape
        self.size = value.size
        self.strides = value.strides


def adjust_linear_filter(op, synapse, num, den, dt, method='zoh'):
    """
    A copy of some of the functionality that gets applied to
    linear filters in refimpl nengo.
    """

    num, den, _ = cont2discrete(
        (num, den), dt, method=method)
    num = num.flatten()
    num = num[1:] if num[0] == 0 else num
    den = den[1:]  # drop first element (equal to 1)

    return num, den


def pyfunc_checks(val):
    """
    If the output can possibly be treated as a scalar, convert it
    to a python float. Otherwise, convert it to a numpy ndarray.
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
    """
    MpiSend placeholder operator. Stores the signal that the operator will
    send and the component that it will be sent to. No makestep is defined,
    as it will never be called.
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
    """
    MpiRecv placeholder operator. Stores the signal that the operator will
    receive and the component that it will be received from. No makestep is
    defined, as it will never be called.
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
    """
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
    conn_ops: A list containing the operators implementing a nengo connection.

    signal: The signal where the connection will be split. Must be updated by
        one of the operators in ``conn_ops''.

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
    """
    Create a key for an object. Must be unique, and reproducable (i.e. produce
    the same key if called with the same object multiple times).
    """
    if isinstance(obj, builder.signal.SignalView):
        return id(obj.base)
    else:
        return id(obj)


def signal_to_string(signal, delim=':'):
    """
    Takes in a signal, and encodes the relevant information in a string.
    The format of the returned string:

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


def ndarray_to_mpi_string(a):
    if a.ndim == 0:
        s = "[1,1]%f" % a

    elif a.ndim == 1:
        s = "[%d,1]" % a.size
        s += ",".join([str(f) for f in a.flatten()])

    else:
        assert a.ndim == 2
        s = "[%d,%d]" % a.shape
        s += ",".join([str(f) for f in a.flatten()])

    return s


class MpiModel(builder.Model):
    """
    Output of the MpiBuilder, used by the Simulator.

    Differs from the Model in the reference implementation in that
    as the model is built, it keeps track of the object currently being
    built. This permits it to track which operators are added as part
    of which high-level objects, so that those operators can later be
    added to the correct MPI component (required since MPI components are
    specified in terms of the high-level nengo objects like nodes,
    networks and ensembles).
    """

    op_string_delim = ";"
    signal_string_delim = ":"
    outfile_delim = "|"

    def __init__(
            self, num_components, assignments, dt=0.001, label=None,
            decoder_cache=NoDecoderCache(), save_file="", free_memory=True):

        self.num_components = num_components
        self.assignments = assignments

        if save_file:
            self.save_file = open(save_file, 'w')
            self.save_file.write(
                "%s%s%s" % (num_components, MpiModel.outfile_delim, dt))
        else:
            self.save_file = None

            import mpi_sim
            self.mpi_sim = mpi_sim.PythonMpiSimulator(num_components, dt)

        # for each component, stores the keys of the signals that have
        # to be sent and received, respectively
        self.send_signals = defaultdict(list)
        self.recv_signals = defaultdict(list)

        # for each component, stores the keys of the signals that have
        # already been added to that component.
        self.added_signals = defaultdict(list)

        # operators for each component
        self.component_ops = defaultdict(list)

        # probe -> C++ key (int)
        # Used to query the C++ simulator for probe data
        self.probe_keys = {}

        self._object_context = [None]
        self.object_ops = defaultdict(list)

        self._mpi_tag = 0

        self.free_memory = free_memory

        super(MpiModel, self).__init__(dt, label, decoder_cache)

    @property
    def runnable(self):
        return self.save_file is None

    def __str__(self):
        return "MpiModel: %s" % self.label

    def get_new_mpi_tag(self):
        mpi_tag = self._mpi_tag
        self._mpi_tag += 1
        return mpi_tag

    def push_object(self, object):
        self._object_context.append(object)

    def pop_object(self):

        obj = self._object_context.pop()

        if not isinstance(obj, Connection):
            component = self.assignments[obj]

            self.add_ops(component, self.object_ops[obj])

        else:
            conn = obj
            pre_component = self.assignments[conn.pre_obj]
            post_component = self.assignments[conn.post_obj]

            if pre_component == post_component:
                self.add_ops(pre_component, self.object_ops[conn])

            else:
                # conn crosses component boundaries
                if conn.modulatory:
                    raise Exception(
                        "Connections crossing component boundaries "
                        "must not be modulatory.")

                if conn.learning_rule_type:
                    raise Exception(
                        "Connections crossing component boundaries "
                        "must not have learning rules.")

                if 'synapse_out' in self.sig[conn]:
                    signal = self.sig[conn]['synapse_out']
                else:
                    raise Exception(
                        "Connections crossing component boundaries "
                        "must be filtered so that there is an update.")

                tag = self.get_new_mpi_tag()

                self.send_signals[pre_component].append(
                    (signal, tag, post_component))
                self.recv_signals[post_component].append(
                    (signal, tag, pre_component))

                pre_ops, post_ops = split_connection(
                    self.object_ops[conn], signal)

                # Have to add the signal to both components, so can't delete it
                # the first time.
                self.add_signal(pre_component, signal, delete=False)
                self.add_signal(
                    post_component, signal, delete=self.free_memory)

                self.add_ops(pre_component, pre_ops)
                self.add_ops(post_component, post_ops)

    def add_ops(self, component, ops):
        for op in ops:
            for signal in op.all_signals:
                self.add_signal(component, signal, delete=self.free_memory)

        self.component_ops[component].extend(ops)

    def add_signal(self, component, signal, delete=True):
        key = make_key(signal)

        if key not in self.added_signals[component]:
            logger.debug(
                "Component %d: Adding signal %s with key: %s",
                component, signal, make_key(signal))

            self.added_signals[component].append(key)

            label = str(signal)

            A = signal.base._value

            if A.ndim == 0:
                A = np.reshape(A, (1, 1))

            if A.dtype != np.float64:
                A = A.astype(np.float64)

            if self.save_file:
                data_string = ndarray_to_mpi_string(A)
                signal_string = MpiModel.outfile_delim.join(
                    str(i)
                    for i
                    in ["SIGNAL", component, key, label, data_string])

                self.save_file.write("\n" + signal_string)
            else:
                self.mpi_sim.add_signal(component, key, label, A)

            if delete:
                # Replace the data stored in the signal by a dummy array,
                # which has no contents but has the same shape, size, etc
                # as the original. This should allow the memory to be
                # reclaimed.
                signal.base._value = DummyNdarray(signal.base._value)

    def add_op(self, op):
        """
        Records that the operator was added as part of building
        the object that is on the top of _object_context stack.
        """
        self.object_ops[self._object_context[-1]].append(op)

    def finalize_build(self):
        """
        Called once the MpiBuilder has finished running. Adds operators
        and probes to the mpi simulator. The signals should already have
        been added by this point; they are added to MPI as soon as they
        are built and then deleted from the python level, to save memory.
        """

        # Do this to throw an exception in case of an invalid graph.
        all_ops = list(chain(
            *[self.component_ops[component]
              for component in range(self.num_components)]))
        dg = operator_depencency_graph(all_ops)
        [node for node in toposort(dg) if hasattr(node, 'make_step')]

        for component in range(self.num_components):
            self.add_ops_to_mpi(component)

        for probe in self.probes:
            self.add_probe(
                probe, self.sig[probe]['in'],
                sample_every=probe.sample_every)

        if not self.save_file:
            self.mpi_sim.finalize_build()

    def from_refimpl_model(self, model):
        """Create an MpiModel from an instance of a refimpl Model."""

        if not isinstance(model, builder.Model):
            raise TypeError(
                "Model must be an instance of "
                "%s." % builder.model.__name__)

        self.dt = model.dt
        self.label = model.label
        self.decoder_cache = model.decoder_cache

        self.toplevel = model.toplevel
        self.config = model.config

        self.operators = model.operators
        self.params = model.params
        self.seeds = model.seeds
        self.probes = model.probes
        self.sig = model.sig

    def add_ops_to_mpi(self, component):
        """
        Adds to MPI all ops that are meant for the given component. Which ops
        are meant for which components is stored in self.component_ops dict.

        For all ops except PyFuncs, creates a string encoding all information
        about the op, and passes it into the C++ MPI simulator.
        """

        send_signals = self.send_signals[component]
        recv_signals = self.recv_signals[component]

        # Required for the dependency-graph-creation to work properly.
        for signal, tag, dst in recv_signals:
            self.component_ops[component].append(
                builder.operator.PreserveValue(signal))

        dg = operator_depencency_graph(self.component_ops[component])
        step_order = [
            node for node in toposort(dg) if hasattr(node, 'make_step')]

        for signal, tag, dst in send_signals:
            mpi_send = MpiSend(dst, tag, signal)

            update_indices = filter(
                lambda i: signal in step_order[i].updates,
                range(len(step_order)))

            assert len(update_indices) == 1

            # Put the send after the op that updates the signal.
            step_order.insert(update_indices[0]+1, mpi_send)

        for signal, tag, src in recv_signals:
            mpi_recv = MpiRecv(src, tag, signal)

            read_indices = filter(
                lambda i: signal in step_order[i].reads,
                range(len(step_order)))

            # Put the recv in front of the first op that reads the signal.
            step_order.insert(read_indices[0], mpi_recv)

        for op in step_order:
            op_type = type(op)

            if op_type == builder.node.SimPyFunc:
                if self.save_file:
                    raise Exception(
                        "Cannot create SimPyFunc operator "
                        "when saving to file.")

                t_in = op.t_in
                fn = op.fn
                x = op.x

                if x is None:
                    if op.output is None:
                        self.mpi_sim.create_PyFunc(fn, t_in)
                    else:
                        self.mpi_sim.create_PyFuncO(
                            make_checked_func(fn, t_in, False),
                            t_in, signal_to_string(op.output))

                else:
                    if isinstance(x.value, DummyNdarray):
                        input_array = np.zeros(x.shape)
                    else:
                        input_array = x.value

                    if op.output is None:
                        self.mpi_sim.create_PyFuncI(
                            fn, t_in, signal_to_string(x), input_array)

                    else:
                        self.mpi_sim.create_PyFuncIO(
                            make_checked_func(fn, t_in, True), t_in,
                            signal_to_string(x), input_array,
                            signal_to_string(op.output))
            else:
                op_string = self.op_to_string(op)

                if op_string:
                    logger.debug(
                        "Component %d: Adding operator with string: %s",
                        component, op_string)

                    if self.save_file:
                        op_string = MpiModel.outfile_delim.join(
                            str(i)
                            for i
                            in ["OP", component, op_string])

                        self.save_file.write("\n" + op_string)
                    else:
                        self.mpi_sim.add_op(component, op_string)

    def op_to_string(self, op):
        """
        Convert an operator into a string. The string will be passed into
        the C++ simulator, where it will be communicated using MPI to the
        correct MPI process. That process will then build an operator
        using the parameters specified in the string.
        """

        op_type = type(op)

        if op_type == builder.operator.Reset:
            op_args = ["Reset", signal_to_string(op.dst), op.value]

        elif op_type == builder.operator.Copy:
            op_args = [
                "Copy", signal_to_string(op.dst), signal_to_string(op.src)]

        elif op_type == builder.operator.DotInc:
            op_args = [
                "DotInc", signal_to_string(op.A), signal_to_string(op.X),
                signal_to_string(op.Y)]

        elif op_type == builder.operator.ElementwiseInc:
            op_args = [
                "ElementwiseInc", signal_to_string(op.A),
                signal_to_string(op.X), signal_to_string(op.Y)]

        elif op_type == builder.neurons.SimNeurons:
            num_neurons = op.J.size
            neuron_type = type(op.neurons)

            if neuron_type is LIF:
                tau_ref = op.neurons.tau_ref
                tau_rc = op.neurons.tau_rc
                op_args = [
                    "LIF", num_neurons, tau_rc, tau_ref, self.dt,
                    signal_to_string(op.J), signal_to_string(op.output)]

            elif neuron_type is LIFRate:
                tau_ref = op.neurons.tau_ref
                tau_rc = op.neurons.tau_rc
                op_args = [
                    "LIFRate", num_neurons, tau_rc, tau_ref,
                    signal_to_string(op.J), signal_to_string(op.output)]

            elif neuron_type is RectifiedLinear:
                op_args = [
                    "RectifiedLinear", num_neurons, signal_to_string(op.J),
                    signal_to_string(op.output)]

            elif neuron_type is Sigmoid:
                op_args = [
                    "Sigmoid", num_neurons, op.neurons.tau_ref,
                    signal_to_string(op.J), signal_to_string(op.output)]
            else:
                raise NotImplementedError(
                    'nengo_mpi cannot handle neurons of type ' +
                    str(neuron_type))

        elif op_type == builder.synapses.SimSynapse:

            synapse = op.synapse

            if isinstance(synapse, LinearFilter):

                do_adjust = not((isinstance(synapse, Alpha) or
                                 isinstance(synapse, Lowpass)) and
                                synapse.tau <= .03 * self.dt)

                if do_adjust:
                    num, den = adjust_linear_filter(
                        op, synapse, synapse.num, synapse.den, self.dt)
                else:
                    num, den = synapse.num, synapse.den

                op_args = [
                    "LinearFilter", signal_to_string(op.input),
                    signal_to_string(op.output), str(list(num)),
                    str(list(den))]

            else:
                raise NotImplementedError(
                    'nengo_mpi cannot handle synapses of '
                    'type %s' % str(type(synapse)))

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
            op_args = ["SpaunStimulus", output, op.stimulus_sequence]

        else:
            raise NotImplementedError(
                "nengo_mpi cannot handle operator of "
                "type %s" % str(op_type))

        op_string = MpiModel.op_string_delim.join(map(str, op_args))
        op_string = op_string.replace(" ", "")
        op_string = op_string.replace("(", "")
        op_string = op_string.replace(")", "")

        return op_string

    def add_probe(self, probe, signal, sample_every=None):
        """Add a probe to the mpi simulator."""

        period = 1 if sample_every is None else sample_every / self.dt

        probe_key = make_key(probe)
        self.probe_keys[probe] = probe_key

        signal_string = signal_to_string(signal)

        component = self.assignments[probe]

        logger.debug(
            "Component: %d: Adding probe of signal %s.\n"
            "probe_key: %d, signal_string: %s, period: %d",
            component, str(signal), probe_key,
            signal_string, period)

        if self.save_file:
            probe_string = MpiModel.outfile_delim.join(
                str(i)
                for i
                in ["PROBE", component, probe_key,
                    signal_string, period, str(probe)])

            self.save_file.write("\n" + probe_string)
        else:
            self.mpi_sim.add_probe(
                component, self.probe_keys[probe],
                signal_string, period, str(probe))
