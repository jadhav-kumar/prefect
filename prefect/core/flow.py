import copy
import hashlib
import inspect
import itertools
import random
import tempfile
import uuid
from collections import Counter
from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    AnyStr,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Union,
)

import graphviz
import jsonpickle
from mypy_extensions import TypedDict

import prefect
import prefect.schedules
from prefect.core.edge import Edge
from prefect.core.task import Parameter, Task
from prefect.core.task_result import TaskResult
from prefect.utilities.functions import cache
from prefect.utilities.json import Serializable
from prefect.utilities.tasks import as_task_result

VAR_POSITIONAL = inspect.Parameter.VAR_POSITIONAL

ParameterDetails = TypedDict("ParameterDetails", {"default": Any, "required": bool})


def flow_cache_key(flow: "Flow") -> int:
    """
    Returns a cache key that can be used to determine if the cache is stale.
    """

    return hash((frozenset(flow.tasks), frozenset(flow.edges)))


class Flow(Serializable):
    def __init__(
        self,
        name: str = None,
        version: str = None,
        schedule: prefect.schedules.Schedule = None,
        description: str = None,
        environment: prefect.environments.Environment = None,
        tasks: Iterable[Task] = None,
        edges: Iterable[Edge] = None,
    ) -> None:

        self.name = name or type(self).__name__
        self.version = version
        self.description = description
        self.schedule = schedule or prefect.schedules.NoSchedule()
        self.environment = environment

        self.tasks = set()  # type: Set[Task]
        self.edges = set()  # type: Set[Edge]

        for t in tasks or []:
            self.add_task(t)

        for e in edges or []:
            self.add_edge(
                upstream_task=e.upstream_task,
                downstream_task=e.downstream_task,
                key=e.key,
            )

        self._prefect_version = prefect.__version__
        self._cache = {}

        super().__init__()

    def __eq__(self, other: Any) -> bool:
        if type(self) == type(other):
            s = (self.name, self.version, self.tasks, self.edges)
            o = (other.name, other.version, other.tasks, other.edges)
            return s == o
        return False

    def __repr__(self) -> str:
        return "<{cls}: {self.name}{v}>".format(
            cls=type(self).__name__,
            self=self,
            v=" version={}".format(self.version) if self.version else "",
        )

    def __iter__(self) -> Iterable[Task]:
        yield from self.sorted_tasks()

    # Identification  ----------------------------------------------------------

    def copy(self) -> "Flow":
        new = copy.copy(self)
        new.tasks = self.tasks.copy()
        new.edges = self.edges.copy()
        return new

    # Context Manager ----------------------------------------------------------

    def __enter__(self) -> "Flow":
        self.__previous_flow = prefect.context.get("flow")
        prefect.context.update(flow=self)
        return self

    def __exit__(self, _type, _value, _tb) -> None:  # type: ignore
        del prefect.context.flow
        if self.__previous_flow is not None:
            prefect.context.update(flow=self.__previous_flow)

        del self.__previous_flow

    # Introspection ------------------------------------------------------------

    @cache(validation_fn=flow_cache_key)
    def root_tasks(self) -> Set[Task]:
        """
        Returns the root tasks of the Flow -- tasks that have no upstream
        dependencies.
        """
        return set(t for t in self.tasks if not self.edges_to(t))

    @cache(validation_fn=flow_cache_key)
    def terminal_tasks(self) -> Set[Task]:
        """
        Returns the terminal tasks of the Flow -- tasks that have no downstream
        dependencies.
        """
        return set(t for t in self.tasks if not self.edges_from(t))

    def parameters(self, only_required=False) -> Dict[str, ParameterDetails]:
        """
        Returns details about any Parameters of this flow
        """
        return {
            t.name: {"required": t.required, "default": t.default}
            for t in self.tasks
            if isinstance(t, Parameter) and (t.required if only_required else True)
        }

    # Graph --------------------------------------------------------------------

    @contextmanager
    def restore_graph_on_error(self, validate: bool = True) -> Iterator[None]:
        """
        A context manager that saves the Flow's graph (tasks & edges) and
        restores it if an error is raised. It can be used to test potentially
        erroneous configurations (for example, ones that might include cycles)
        without modifying the graph.

        It will automatically check for cycles when restored.

        with flow.restore_graph_on_error():
            # this will raise an error, but the flow graph will not be modified
            add_cycle_to_graph(flow)
        """
        tasks, edges = self.tasks.copy(), self.edges.copy()
        try:
            yield
            if validate:
                self.validate()
        except Exception:
            self.tasks, self.edges = tasks, edges
            raise

    def add_task(self, task: Task) -> None:
        if not isinstance(task, Task):
            raise TypeError(
                "Tasks must be Task instances (received {})".format(type(task))
            )
        elif task not in self.tasks:
            if task.slug and task.slug in [t.slug for t in self.tasks]:
                raise ValueError(
                    'A task with the slug "{}" already exists in this '
                    "flow.".format(task.slug)
                )

        self.tasks.add(task)

    def add_edge(
        self,
        upstream_task: Task,
        downstream_task: Task,
        key: str = None,
        validate: bool = True,
    ) -> None:
        if isinstance(upstream_task, TaskResult):
            upstream_task = upstream_task.task
        if isinstance(downstream_task, TaskResult):
            downstream_task = downstream_task.task
        if isinstance(downstream_task, Parameter):
            raise ValueError("Parameters can not have upstream dependencies.")

        if key and key in {e.key for e in self.edges_to(downstream_task)}:
            raise ValueError(
                'Argument "{a}" for task {t} has already been assigned in '
                "this flow. If you are trying to call the task again with "
                "new arguments, call Task.copy() before adding the result "
                "to this flow.".format(a=key, t=downstream_task)
            )

        edge = Edge(
            upstream_task=upstream_task, downstream_task=downstream_task, key=key
        )

        with self.restore_graph_on_error(validate=validate):
            if upstream_task not in self.tasks:
                self.add_task(upstream_task)
            if downstream_task not in self.tasks:
                self.add_task(downstream_task)
            self.edges.add(edge)

            # check that the edges are valid keywords by binding them
            if key is not None:
                edge_keys = {
                    e.key: None
                    for e in self.edges_to(downstream_task)
                    if e.key is not None
                }
                inspect.signature(downstream_task.run).bind_partial(**edge_keys)

    def update(self, flow: "Flow", validate: bool = True) -> None:
        with self.restore_graph_on_error(validate=validate):

            for task in flow.tasks:
                if task not in self.tasks:
                    self.add_task(task)

            for edge in flow.edges:
                if edge not in self.edges:
                    self.add_edge(
                        upstream_task=edge.upstream_task,
                        downstream_task=edge.downstream_task,
                        key=edge.key,
                        validate=False,
                    )

    def add_task_results(
        self, *task_results: TaskResult, validate: bool = True
    ) -> None:
        with self.restore_graph_on_error(validate=validate):
            for t in task_results:
                self.add_task(t.task)
                self.update(t.flow, validate=False)

    @cache(validation_fn=flow_cache_key)
    def all_upstream_edges(self) -> Dict[Task, Set[Edge]]:
        edges = {t: set() for t in self.tasks}  # type: Dict[Task, Set[Edge]]
        for edge in self.edges:
            edges[edge.downstream_task].add(edge)
        return edges

    @cache(validation_fn=flow_cache_key)
    def all_downstream_edges(self) -> Dict[Task, Set[Edge]]:
        edges = {t: set() for t in self.tasks}  # type: Dict[Task, Set[Edge]]
        for edge in self.edges:
            edges[edge.upstream_task].add(edge)
        return edges

    def edges_to(self, task: Task) -> Set[Edge]:
        return self.all_upstream_edges()[task]

    def edges_from(self, task: Task) -> Set[Edge]:
        return self.all_downstream_edges()[task]

    def upstream_tasks(self, task: Task) -> Set[Task]:
        return set(e.upstream_task for e in self.edges_to(task))

    def downstream_tasks(self, task: Task) -> Set[Task]:
        return set(e.downstream_task for e in self.edges_from(task))

    def validate(self) -> None:
        """
        Checks the flow for cycles and raises an error if one is found.
        """
        self.sorted_tasks()

    @cache(validation_fn=flow_cache_key)
    def sorted_tasks(self, root_tasks: Iterable[Task] = None) -> Tuple[Task, ...]:

        # begin by getting all tasks under consideration (root tasks and all
        # downstream tasks)
        if root_tasks:
            tasks = set(root_tasks)
            seen = set()  # type: Set[Task]

            # while the set of tasks is different from the seen tasks...
            while tasks.difference(seen):
                # iterate over the new tasks...
                for t in list(tasks.difference(seen)):
                    # add its downstream tasks to the task list
                    tasks.update(self.downstream_tasks(t))
                    # mark it as seen
                    seen.add(t)
        else:
            tasks = self.tasks

        # build the list of sorted tasks
        remaining_tasks = list(tasks)
        sorted_tasks = []
        while remaining_tasks:
            # mark the flow as cyclic unless we prove otherwise
            cyclic = True

            # iterate over each remaining task
            for task in remaining_tasks.copy():
                # check all the upstream tasks of that task
                for upstream_task in self.upstream_tasks(task):
                    # if the upstream task is also remaining, it means it
                    # hasn't been sorted, so we can't sort this task either
                    if upstream_task in remaining_tasks:
                        break
                else:
                    # but if all upstream tasks have been sorted, we can sort
                    # this one too. We note that we found no cycle this time.
                    cyclic = False
                    remaining_tasks.remove(task)
                    sorted_tasks.append(task)

            # if we were unable to match any upstream tasks, we have a cycle
            if cyclic:
                raise ValueError("Flows must be acyclic!")

        return tuple(sorted_tasks)

    # Dependencies ------------------------------------------------------------

    def set_dependencies(
        self,
        task: Task,
        upstream_tasks: Iterable[Task] = None,
        downstream_tasks: Iterable[Task] = None,
        keyword_results: Mapping[str, Task] = None,
        validate: bool = True,
    ) -> TaskResult:
        """
        Convenience function for adding task dependencies on upstream tasks.

        Args:
            task (Task): a Task that will become part of the Flow

            upstream_tasks ([Task]): Tasks that will run before the task runs

            downstream_tasks ([Task]): Tasks that will run after the task runs

            keyword_results ({key: Task}): The results of these tasks
                will be provided to the task under the specified keyword
                arguments.
        """
        with self.restore_graph_on_error(validate=validate):

            result = as_task_result(task)  # type: TaskResult
            task = result.task

            # validate the task
            signature = inspect.signature(task.run)
            varargs = next(
                (p for p in signature.parameters.values() if p.kind == VAR_POSITIONAL),
                None,
            )

            if varargs:
                raise ValueError(
                    "Tasks with variable positional arguments (*args) are not "
                    "supported, because all Prefect arguments are stored as "
                    "keywords. As a workaround, consider modifying the run() "
                    "method to accept **kwargs and feeding the values "
                    "to *args."
                )

            # update this flow with the result
            self.add_task_results(result)

            for t in upstream_tasks or []:
                tr = as_task_result(t)
                self.add_task_results(tr)
                self.add_edge(upstream_task=tr, downstream_task=task, validate=False)

            for t in downstream_tasks or []:
                tr = as_task_result(t)
                self.add_task_results(tr)
                self.add_edge(upstream_task=task, downstream_task=tr, validate=False)

            for key, t in (keyword_results or {}).items():
                tr = as_task_result(t)
                self.add_task_results(tr)
                self.add_edge(
                    upstream_task=tr, downstream_task=task, key=key, validate=False
                )

        return TaskResult(task=task, flow=self)

    # Execution  ---------------------------------------------------------------

    def run(self, parameters=None, executor=None, **kwargs):
        """
        Run the flow.
        """
        runner = prefect.engine.flow_runner.FlowRunner(flow=self, executor=executor)

        parameters = parameters or {}
        for p in self.parameters():
            if p in kwargs:
                parameters[p] = kwargs.pop(p)

        return runner.run(parameters=parameters, **kwargs)

    # Serialization ------------------------------------------------------------

    def serialize(self, seed=None) -> dict:
        ref_ids = self.fingerprint(seed=seed)

        return dict(
            ref_id=ref_ids["flow_id"],
            name=self.name,
            version=self.version,
            description=self.description,
            parameters=self.parameters(),
            schedule=self.schedule,
            tasks=[
                dict(ref_id=ref_ids["task_ids"][t], **t.serialize()) for t in self.tasks
            ],
            edges=[
                dict(
                    upstream_ref_id=ref_ids["task_ids"][e.upstream_task],
                    downstream_ref_id=ref_ids["task_ids"][e.downstream_task],
                    key=e.key,
                )
                for e in self.edges
            ],
        )

    # Visualization ------------------------------------------------------------

    def visualize(self):
        graph = graphviz.Digraph()

        for t in self.tasks:
            graph.node(str(id(t)), t.name)

        for e in self.edges:
            graph.edge(str(id(e.upstream_task)), str(id(e.downstream_task)), e.key)

        with tempfile.NamedTemporaryFile() as tmp:
            graph.render(tmp.name, view=True)

    # IDs ------------------------------------------------------------

    def generate_flow_id(self) -> str:
        """
        Flows are identified by their name and version.
        """
        hash_bytes = get_hash("{}:{}".format(self.name, self.version or ""))
        return str(uuid.UUID(bytes=hash_bytes))

    def generate_task_ids(self) -> Dict["Task", str]:
        final_hashes = {}

        # --- initial pass
        # for each task, generate a hash based on that task's attributes
        hashes = {t: get_hash(t) for t in self.tasks}
        counter = Counter(hashes.values())

        # --- forward pass #1
        # for each task in order:
        # - if the task hash is unique, put it in final_hashes
        # - if not, hash the task with the hash of all incoming edges
        # the result is a hash that represents each task in terms of all ancestors since the
        # last uniquely defined task.
        for t in self.sorted_tasks():
            if counter[hashes[t]] == 1:
                final_hashes[t] = hashes[t]
                continue

            edge_hashes = sorted(
                (e.key, hashes[e.upstream_task]) for e in self.edges_to(t)
            )
            hashes[t] = get_hash((hashes[t], edge_hashes))
            counter[hashes[t]] += 1

        # --- backward pass
        # for each task in reverse order:
        # - if the task hash is unique, put it in final_hashes
        # - if not, hash the task with the hash of all outgoing edges
        # the result is a hash that represents each task in terms of both its ancestors (from
        # the foward pass) and also any descendents.
        for t in reversed(self.sorted_tasks()):
            if counter[hashes[t]] == 1:
                final_hashes[t] = hashes[t]
                continue

            edge_hashes = sorted(
                (e.key, hashes[e.downstream_task]) for e in self.edges_from(t)
            )
            hashes[t] = get_hash(str((hashes[t], edge_hashes)))
            counter[hashes[t]] += 1

        # --- forward pass #2
        # for each task in order:
        # - if the task hash is unique, put it in final_hashes
        # if not, hash the task with the hash of all incoming edges.
        # define each task in terms of the computational path of every task it's
        # connected to
        #
        # any task that is still a duplicate at this stage is TRULY a duplicate;
        # there is nothing about its computational path that differentiates it.
        # We can randomly choose one and modify its hash (and the hash of all
        # following tasks) without consequence.
        for t in self.sorted_tasks():
            if counter[hashes[t]] == 1:
                final_hashes[t] = hashes[t]
                continue

            edge_hashes = sorted(
                (e.key, hashes[e.upstream_task]) for e in self.edges_to(t)
            )
            hashes[t] = get_hash(str((hashes[t], edge_hashes)))
            counter[hashes[t]] += 1

            # duplicate check
            while counter[hashes[t]] > 1:
                hashes[t] = get_hash(hashes[t])
                counter[hashes[t]] += 1
            final_hashes[t] = hashes[t]

        seed = uuid.UUID(self.generate_flow_id()).bytes

        return {t: str(uuid.UUID(bytes=xor(seed, h))) for t, h in final_hashes.items()}


def get_hash(obj: object) -> bytes:
    """
    Returns a deterministic set of bytes for a given input.
    """
    if isinstance(obj, bytes):
        return hashlib.md5(obj).digest()
    elif isinstance(obj, str):
        return hashlib.md5(obj.encode()).digest()
    elif isinstance(obj, Task):
        obj = prefect.core.task.get_task_info(obj)
    elif isinstance(obj, Edge):
        obj = dict(
            upstream=prefect.core.task.get_task_info(obj.upstream_task),
            downstream=prefect.core.task.get_task_info(obj.downstream_task),
            key=obj.key,
        )
    return hashlib.md5(jsonpickle.dumps(obj).encode()).digest()


def xor(hash1: bytes, hash2: bytes) -> bytes:
    """
    Computes the bitwise XOR between two byte hashes
    """
    return bytes([x ^ y for x, y in zip(hash1, itertools.cycle(hash2))])
