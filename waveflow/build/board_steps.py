"""Optional board-packaging steps for the existing BuildDag."""
from __future__ import annotations

from dataclasses import dataclass, field

from waveflow.build.build import BuildConfig, BuildStep
from waveflow.build.ip_export import IpExportSpec, export_ip, _new_run, _sha, _write_json
from waveflow.build.hls_control import control_spec_from_export
from waveflow.build.host_driver import generate_host_driver
from waveflow.hw.regmap import VitisRegMap


@dataclass(kw_only=True)
class ExportIpStep(BuildStep):
    """Export a synthesis receipt into a new, validated IP package.

    Each run returns paths to a unique package, so outputs are in-memory path
    artifacts, not statically cached files. With the existing BuildDag contract,
    explicitly request dag.run(config, force=[step.name]) for a standalone export,
    or let a running downstream file-producing step demand these artifacts.
    'through' selects graph nodes; it does not force an in-memory-only step.

    The producer of receipt_artifact supplies the receipt returned by
    synthesize_for_board(). No example, shared cache policy, or synthesis DAG
    is changed here.
    """

    description = "Export and verify a self-describing Vivado IP package."
    spec: IpExportSpec
    receipt_artifact: str = "hls_synthesis_receipt"
    output_dir: str = "board_build/exports"
    directory_artifact: str = "ip_directory"
    archive_artifact: str = "ip_archive"
    manifest_artifact: str = "ip_manifest"

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.spec, IpExportSpec):
            raise TypeError("spec must be an IpExportSpec")
        names = (self.receipt_artifact, self.directory_artifact,
                 self.archive_artifact, self.manifest_artifact)
        if len(set(names)) != 4 or any(not isinstance(name, str) or not name for name in names):
            raise ValueError("IP input/output artifact names must be nonempty and distinct")

    @property
    def consumes(self):
        return [self.receipt_artifact]

    @property
    def produces(self):
        return {self.directory_artifact: None, self.archive_artifact: None,
                self.manifest_artifact: None}

    def run(self, config: BuildConfig, **kwargs):
        result = export_ip(
            config, kwargs[self.receipt_artifact], self.spec,
            work_dir=config.root_dir / self.output_dir,
        )
        return {self.directory_artifact: result.directory, self.archive_artifact: result.archive,
                self.manifest_artifact: result.manifest}


@dataclass(kw_only=True)
class HlsControlSpecStep(BuildStep):
    """Verify S0b artifacts and scalar/array intent before publishing a control ABI.

    Like ExportIpStep, unique-run outputs use the existing in-memory artifact
    contract. Use explicit force for standalone execution; no mtime ABI cache.
    """

    description = "Verify the generated HLS control ABI against register design intent."
    regmap: VitisRegMap
    interface: str = "s_axi_control"
    pointer_bindings: dict[str, str] = field(default_factory=dict)
    manifest_artifact: str = "ip_manifest"
    spec_artifact: str = "hls_control_spec"
    abi_artifact: str = "hls_control_manifest"
    output_dir: str = "board_build/control"

    @property
    def consumes(self):
        return [self.manifest_artifact]

    @property
    def produces(self):
        return {self.spec_artifact: None, self.abi_artifact: None}

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.regmap, VitisRegMap):
            raise TypeError("regmap must be a VitisRegMap")
        names = (self.manifest_artifact, self.spec_artifact, self.abi_artifact)
        if len(set(names)) != len(names) or any(not isinstance(n, str) or not n for n in names):
            raise ValueError("ABI artifact names must be nonempty and distinct")
        self.pointer_bindings = dict(self.pointer_bindings)

    def run(self, config: BuildConfig, **kwargs):
        manifest = kwargs[self.manifest_artifact]
        before = _sha(manifest)
        spec = control_spec_from_export(config, manifest, self.regmap,
                                        interface=self.interface, pointer_bindings=self.pointer_bindings)
        if _sha(manifest) != before:
            raise ValueError("export manifest changed during ABI verification")
        run = _new_run(config.root_dir / self.output_dir, "abi-")
        path = run / "control_abi.json"
        _write_json(path, {**spec.to_dict(), "abi_sha256": spec.abi_sha256,
                           "ip_manifest_sha256": before})
        return {self.spec_artifact: spec, self.abi_artifact: path}


@dataclass(kw_only=True)
class HostDriverGenStep(BuildStep):
    """Generate a stdlib-only driver using the verified spec, not a RegMap formula."""

    description = "Generate a portable host driver from the verified HLS ABI."
    spec_artifact: str = "hls_control_spec"
    driver_artifact: str = "host_driver"
    manifest_artifact: str = "host_driver_manifest"
    output_dir: str = "board_build/host"
    class_name: str = "Driver"

    @property
    def consumes(self):
        return [self.spec_artifact]

    @property
    def produces(self):
        return {self.driver_artifact: None, self.manifest_artifact: None}

    def __post_init__(self):
        super().__post_init__()
        names = (self.spec_artifact, self.driver_artifact, self.manifest_artifact)
        if len(set(names)) != len(names) or any(not isinstance(n, str) or not n for n in names):
            raise ValueError("driver artifact names must be nonempty and distinct")

    def run(self, config: BuildConfig, **kwargs):
        result = generate_host_driver(kwargs[self.spec_artifact], config.root_dir / self.output_dir,
                                      class_name=self.class_name)
        return {self.driver_artifact: result.driver, self.manifest_artifact: result.manifest}


# S0d keeps the existing unique-run/in-memory artifact contract. Explicit force
# is required when selecting these standalone steps; shared cache policy is unchanged.
from waveflow.build.vivado_board import (
    BdDesign, prepare_bd, validate_bd, build_bitstream, collect_overlay,
)


def _vivado_artifacts(step):
    names = [*step.consumes, *step.produces]
    if len(set(names)) != len(names) or any(not isinstance(n, str) or not n for n in names):
        raise ValueError("Vivado artifact names must be nonempty and distinct")


@dataclass(kw_only=True)
class BdGenStep(BuildStep):
    description = "Stage a relocatable board/IP bundle and render the committed BD template."
    design: BdDesign
    ip_artifact: str = "ip_manifest"
    abi_artifact: str = "hls_control_spec"
    inputs_artifact: str = "bd_inputs"
    output_dir: str = "board_build/bd"

    @property
    def consumes(self):
        return [self.ip_artifact, self.abi_artifact]

    @property
    def produces(self):
        return {self.inputs_artifact: None}

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.design, BdDesign):
            raise TypeError("design must be a BdDesign")
        _vivado_artifacts(self)

    def run(self, config, **kwargs):
        path = prepare_bd(config, kwargs[self.ip_artifact], kwargs[self.abi_artifact], self.design,
                          work_dir=config.root_dir / self.output_dir)
        return {self.inputs_artifact: path}


@dataclass(kw_only=True)
class BdValidateStep(BuildStep):
    description = "Validate an isolated Vivado block design without synthesis."
    inputs_artifact: str = "bd_inputs"
    validation_artifact: str = "bd_validation"
    output_dir: str = "board_build/validation"

    @property
    def consumes(self):
        return [self.inputs_artifact]

    @property
    def produces(self):
        return {self.validation_artifact: None}

    def __post_init__(self):
        super().__post_init__()
        _vivado_artifacts(self)

    def run(self, config, **kwargs):
        return {self.validation_artifact: validate_bd(
            config, kwargs[self.inputs_artifact], work_dir=config.root_dir / self.output_dir)}


@dataclass(kw_only=True)
class BitstreamStep(BuildStep):
    description = "Implement a validated BD and verify BIT/HWH/XSA, timing, and DRC."
    validation_artifact: str = "bd_validation"
    bitstream_artifact: str = "bitstream_manifest"
    output_dir: str = "board_build/implementation"

    @property
    def consumes(self):
        return [self.validation_artifact]

    @property
    def produces(self):
        return {self.bitstream_artifact: None}

    def __post_init__(self):
        super().__post_init__()
        _vivado_artifacts(self)

    def run(self, config, **kwargs):
        return {self.bitstream_artifact: build_bitstream(
            config, kwargs[self.validation_artifact], work_dir=config.root_dir / self.output_dir)}


@dataclass(kw_only=True)
class CollectOverlayStep(BuildStep):
    description = "Collect receipt-bound overlay artifacts and their reproducibility manifest."
    bitstream_artifact: str = "bitstream_manifest"
    driver_artifact: str = "host_driver_manifest"
    overlay_artifact: str = "overlay_manifest"
    output_dir: str = "board_build/overlays"

    @property
    def consumes(self):
        return [self.bitstream_artifact, self.driver_artifact]

    @property
    def produces(self):
        return {self.overlay_artifact: None}

    def __post_init__(self):
        super().__post_init__()
        _vivado_artifacts(self)

    def run(self, config, **kwargs):
        return {self.overlay_artifact: collect_overlay(
            config, kwargs[self.bitstream_artifact], kwargs[self.driver_artifact],
            work_dir=config.root_dir / self.output_dir)}
