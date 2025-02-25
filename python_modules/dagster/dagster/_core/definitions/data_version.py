from __future__ import annotations

from collections import OrderedDict
from enum import Enum
from hashlib import sha256
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Iterator,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from typing_extensions import Final

from dagster import _check as check
from dagster._annotations import deprecated
from dagster._utils.cached_method import cached_method

if TYPE_CHECKING:
    from dagster._core.definitions.asset_graph import AssetGraph
    from dagster._core.definitions.events import (
        AssetKey,
        AssetMaterialization,
        AssetObservation,
        Materialization,
    )
    from dagster._core.events.log import EventLogEntry
    from dagster._core.instance import DagsterInstance


class UnknownValue:
    pass


def foo(x):
    return False


UNKNOWN_VALUE: Final[UnknownValue] = UnknownValue()


class DataVersion(
    NamedTuple(
        "_DataVersion",
        [("value", str)],
    )
):
    """(Experimental) Represents a data version for an asset.

    Args:
        value (str): An arbitrary string representing a data version.
    """

    def __new__(
        cls,
        value: str,
    ):
        return super(DataVersion, cls).__new__(
            cls,
            value=check.str_param(value, "value"),
        )


DEFAULT_DATA_VERSION: Final[DataVersion] = DataVersion("INITIAL")
NULL_DATA_VERSION: Final[DataVersion] = DataVersion("NULL")
UNKNOWN_DATA_VERSION: Final[DataVersion] = DataVersion("UNKNOWN")


class DataProvenance(
    NamedTuple(
        "_DataProvenance",
        [
            ("code_version", str),
            ("input_data_versions", Mapping["AssetKey", DataVersion]),
        ],
    )
):
    """(Experimental) Provenance information for an asset materialization.

    Args:
        code_version (str): The code version of the op that generated a materialization.
        input_data_versions (Mapping[AssetKey, DataVersion]): The data versions of the
            inputs used to generate a materialization.
    """

    def __new__(
        cls,
        code_version: str,
        input_data_versions: Mapping["AssetKey", DataVersion],
    ):
        from dagster._core.definitions.events import AssetKey

        return super(DataProvenance, cls).__new__(
            cls,
            code_version=check.str_param(code_version, "code_version"),
            input_data_versions=check.mapping_param(
                input_data_versions,
                "input_data_versions",
                key_type=AssetKey,
                value_type=DataVersion,
            ),
        )

    @staticmethod
    def from_tags(tags: Mapping[str, str]) -> Optional[DataProvenance]:
        from dagster._core.definitions.events import AssetKey

        code_version = tags.get(CODE_VERSION_TAG)
        if code_version is None:
            return None
        input_data_versions = {
            # Everything after the 2nd slash is the asset key
            AssetKey.from_user_string(k.split("/", maxsplit=2)[-1]): DataVersion(tags[k])
            for k, v in tags.items()
            if k.startswith(INPUT_DATA_VERSION_TAG_PREFIX)
            or k.startswith(_OLD_INPUT_DATA_VERSION_TAG_PREFIX)
        }
        return DataProvenance(code_version, input_data_versions)

    @property
    @deprecated
    def input_logical_versions(self) -> Mapping["AssetKey", DataVersion]:
        return self.input_data_versions


# ########################
# ##### TAG KEYS
# ########################

DATA_VERSION_TAG: Final[str] = "dagster/data_version"
_OLD_DATA_VERSION_TAG: Final[str] = "dagster/logical_version"
CODE_VERSION_TAG: Final[str] = "dagster/code_version"
INPUT_DATA_VERSION_TAG_PREFIX: Final[str] = "dagster/input_data_version"
_OLD_INPUT_DATA_VERSION_TAG_PREFIX: Final[str] = "dagster/input_logical_version"
INPUT_EVENT_POINTER_TAG_PREFIX: Final[str] = "dagster/input_event_pointer"


def read_input_data_version_from_tags(
    tags: Mapping[str, str], input_key: "AssetKey"
) -> Optional[DataVersion]:
    value = tags.get(
        get_input_data_version_tag(input_key, prefix=INPUT_DATA_VERSION_TAG_PREFIX)
    ) or tags.get(get_input_data_version_tag(input_key, prefix=_OLD_INPUT_DATA_VERSION_TAG_PREFIX))
    return DataVersion(value) if value is not None else None


def get_input_data_version_tag(
    input_key: "AssetKey", prefix: str = INPUT_DATA_VERSION_TAG_PREFIX
) -> str:
    return f"{prefix}/{input_key.to_user_string()}"


def get_input_event_pointer_tag(input_key: "AssetKey") -> str:
    return f"{INPUT_EVENT_POINTER_TAG_PREFIX}/{input_key.to_user_string()}"


# ########################
# ##### COMPUTE / EXTRACT
# ########################


def compute_logical_data_version(
    code_version: Union[str, UnknownValue],
    input_data_versions: Mapping["AssetKey", DataVersion],
) -> DataVersion:
    """Compute a data version for a value as a hash of input data versions and code version.

    Args:
        code_version (str): The code version of the computation.
        input_data_versions (Mapping[AssetKey, DataVersion]): The data versions of the inputs.

    Returns:
        DataVersion: The computed version as a `DataVersion`.
    """
    from dagster._core.definitions.events import AssetKey

    check.inst_param(code_version, "code_version", (str, UnknownValue))
    check.mapping_param(
        input_data_versions, "input_versions", key_type=AssetKey, value_type=DataVersion
    )

    if (
        isinstance(code_version, UnknownValue)
        or UNKNOWN_DATA_VERSION in input_data_versions.values()
    ):
        return UNKNOWN_DATA_VERSION

    ordered_input_versions = [
        input_data_versions[k] for k in sorted(input_data_versions.keys(), key=str)
    ]
    all_inputs = (code_version, *(v.value for v in ordered_input_versions))

    hash_sig = sha256()
    hash_sig.update(bytearray("".join(all_inputs), "utf8"))
    return DataVersion(hash_sig.hexdigest())


def extract_data_version_from_entry(
    entry: EventLogEntry,
) -> Optional[DataVersion]:
    event_data = _extract_event_data_from_entry(entry)
    tags = event_data.tags or {}
    value = tags.get(DATA_VERSION_TAG) or tags.get(_OLD_DATA_VERSION_TAG)
    return None if value is None else DataVersion(value)


def extract_data_provenance_from_entry(
    entry: EventLogEntry,
) -> Optional[DataProvenance]:
    event_data = _extract_event_data_from_entry(entry)
    tags = event_data.tags or {}
    return DataProvenance.from_tags(tags)


def _extract_event_data_from_entry(
    entry: EventLogEntry,
) -> Union["AssetMaterialization", "AssetObservation"]:
    from dagster._core.definitions.events import AssetMaterialization, AssetObservation
    from dagster._core.events import AssetObservationData, StepMaterializationData

    data = check.not_none(entry.dagster_event).event_specific_data
    event_data: Union[Materialization, AssetMaterialization, AssetObservation]
    if isinstance(data, StepMaterializationData):
        event_data = data.materialization
    elif isinstance(data, AssetObservationData):
        event_data = data.asset_observation
    else:
        check.failed(f"Unexpected event type {type(data)}")

    assert isinstance(event_data, (AssetMaterialization, AssetObservation))
    return event_data


# ########################
# ##### STALENESS OPERATIONS
# ########################


class StaleStatus(Enum):
    MISSING = "MISSING"
    STALE = "STALE"
    FRESH = "FRESH"


class StaleCause(NamedTuple):
    key: AssetKey
    reason: str
    dependency: Optional[AssetKey] = None
    children: Optional[Sequence["StaleCause"]] = None


class CachingStaleStatusResolver:
    """Used to resolve data version information. Avoids redundant database
    calls that would otherwise occur. Intended for use within the scope of a
    single "request" (e.g. GQL request, RunRequest resolution).
    """

    _instance: "DagsterInstance"
    _asset_graph: Optional["AssetGraph"]
    _asset_graph_load_fn: Optional[Callable[[], "AssetGraph"]]

    def __init__(
        self,
        instance: "DagsterInstance",
        asset_graph: Union["AssetGraph", Callable[[], "AssetGraph"]],
    ):
        from dagster._core.definitions.asset_graph import AssetGraph

        self._instance = instance
        if isinstance(asset_graph, AssetGraph):
            self._asset_graph = asset_graph
            self._asset_graph_load_fn = None
        else:
            self._asset_graph = None
            self._asset_graph_load_fn = asset_graph

    def get_status(self, key: AssetKey) -> StaleStatus:
        return self._get_status(key=key)

    def get_stale_causes(self, key: AssetKey) -> Sequence[StaleCause]:
        return self._get_stale_causes(key=key)

    def get_stale_root_causes(self, key: AssetKey) -> Sequence[StaleCause]:
        return self._get_stale_root_causes(key=key)

    def get_current_data_version(self, key: AssetKey) -> DataVersion:
        return self._get_current_data_version(key=key)

    @cached_method
    def _get_status(self, key: AssetKey) -> StaleStatus:
        current_version = self._get_current_data_version(key=key)
        if current_version == NULL_DATA_VERSION:
            return StaleStatus.MISSING
        elif self.asset_graph.is_source(key) or self._is_partitioned_or_downstream(key=key):
            return StaleStatus.FRESH
        else:
            causes = self._get_stale_causes(key=key)
            return StaleStatus.FRESH if len(causes) == 0 else StaleStatus.STALE

    @cached_method
    def _get_stale_causes(self, key: AssetKey) -> Sequence[StaleCause]:
        current_version = self._get_current_data_version(key=key)
        if (
            current_version == NULL_DATA_VERSION
            or self.asset_graph.is_source(key)
            or self._is_partitioned_or_downstream(key=key)
        ):
            return []
        else:
            return list(self._get_stale_causes_materialized(key))

    def _get_stale_causes_materialized(self, key: AssetKey) -> Iterator[StaleCause]:
        code_version = self.asset_graph.get_code_version(key)
        provenance = self._get_current_data_provenance(key=key)
        dependency_keys = self.asset_graph.get_parents(key)

        # only used if no provenance available
        materialization = check.not_none(self._get_latest_materialization_event(key=key))
        materialization_time = materialization.timestamp

        if provenance:
            if code_version and code_version != provenance.code_version:
                yield StaleCause(key, "updated code version")

            removed_deps = set(provenance.input_data_versions.keys()) - set(dependency_keys)
            for dep_key in removed_deps:
                yield StaleCause(
                    key,
                    "removed dependency",
                    dep_key,
                )

        for dep_key in sorted(dependency_keys):
            if self._get_status(key=dep_key) == StaleStatus.STALE:
                yield StaleCause(
                    key,
                    "stale dependency",
                    dep_key,
                    self._get_stale_causes(key=dep_key),
                )
            elif provenance:
                if dep_key not in provenance.input_data_versions:
                    yield StaleCause(
                        key,
                        "new dependency",
                        dep_key,
                    )
                elif provenance.input_data_versions[dep_key] != self._get_current_data_version(
                    key=dep_key
                ):
                    yield StaleCause(
                        key,
                        "updated dependency data version",
                        dep_key,
                        [
                            StaleCause(
                                dep_key,
                                "updated data version",
                            )
                        ],
                    )
            # if no provenance, then use materialization timestamps instead of versions
            # this should be removable eventually since provenance is on all newer materializations
            else:
                dep_materialization = self._get_latest_materialization_event(key=dep_key)
                if dep_materialization is None:
                    # The input must be new if it has no materialization
                    yield StaleCause(key, "new input", dep_key)
                elif dep_materialization.timestamp > materialization_time:
                    yield StaleCause(
                        key,
                        "updated dependency timestamp",
                        dep_key,
                        [
                            StaleCause(
                                dep_key,
                                "updated timestamp",
                            )
                        ],
                    )

    @cached_method
    def _get_stale_root_causes(self, key: AssetKey) -> Sequence[StaleCause]:
        causes = self._get_stale_causes(key=key)
        root_pairs = sorted([pair for cause in causes for pair in self._gather_leaves(cause)])
        # After sorting the pairs, we can drop the level and de-dup using an
        # ordered dict as an ordered set. This will give us unique root causes,
        # sorted by level.
        roots: Dict[StaleCause, None] = OrderedDict()
        for root_cause in [leaf_cause for _, leaf_cause in root_pairs]:
            roots[root_cause] = None
        return list(roots.keys())

    # The leaves of the cause tree for an asset are the root causes of its staleness.
    def _gather_leaves(self, cause: StaleCause, level: int = 0) -> Iterator[Tuple[int, StaleCause]]:
        if cause.children is None:
            yield (level, cause)
        else:
            for child in cause.children:
                yield from self._gather_leaves(child, level=level + 1)

    @property
    def asset_graph(self) -> "AssetGraph":
        if self._asset_graph is None:
            self._asset_graph = check.not_none(self._asset_graph_load_fn)()
        return self._asset_graph

    @cached_method
    def _get_current_data_version(self, *, key: AssetKey) -> DataVersion:
        is_source = self.asset_graph.is_source(key)
        event = self._instance.get_latest_data_version_record(
            key,
            is_source,
        )
        if event is None and is_source:
            return DEFAULT_DATA_VERSION
        elif event is None:
            return NULL_DATA_VERSION
        else:
            data_version = extract_data_version_from_entry(event.event_log_entry)
            return data_version or DEFAULT_DATA_VERSION

    @cached_method
    def _get_latest_materialization_event(self, *, key: AssetKey) -> Optional[EventLogEntry]:
        return self._instance.get_latest_materialization_event(key)

    @cached_method
    def _get_current_data_provenance(self, *, key: AssetKey) -> Optional[DataProvenance]:
        materialization = self._get_latest_materialization_event(key=key)
        if materialization is None:
            return None
        else:
            return extract_data_provenance_from_entry(materialization)

    @cached_method
    def _is_partitioned_or_downstream(self, *, key: AssetKey) -> bool:
        if self.asset_graph.get_partitions_def(key):
            return True
        elif self.asset_graph.is_source(key):
            return False
        else:
            return any(
                self._is_partitioned_or_downstream(key=dep_key)
                for dep_key in self.asset_graph.get_parents(key)
            )

    # Volatility means that an asset is assumed to be constantly changing. We assume that observable
    # source assets are non-volatile, since the primary purpose of the observation function is to
    # determine if a source asset has changed. We assume that regular assets are volatile if they
    # are at the root of the graph (have no dependencies) or are downstream of a volatile asset.
    @cached_method
    def _is_volatile(self, *, key: AssetKey) -> bool:
        if self.asset_graph.is_source(key):
            return self.asset_graph.is_observable(key)
        else:
            deps = self.asset_graph.get_parents(key)
            return len(deps) == 0 or any(self._is_volatile(key=dep_key) for dep_key in deps)
