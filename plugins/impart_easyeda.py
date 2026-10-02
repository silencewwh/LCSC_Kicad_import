# Simplified EasyEDA to KiCad importer
# Based on: https://github.com/uPesy/easyeda2kicad.py/blob/master/easyeda2kicad/__main__.py
from __future__ import annotations

import gzip
import logging
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, NamedTuple

# Configure logging
logger = logging.getLogger(__name__)

current_dir = Path(__file__).resolve().parent
easyeda_submodule = current_dir / "easyeda2kicad"

if easyeda_submodule.exists():
    # Remove any conflicting easyeda2kicad modules from cache first
    modules_to_remove = [k for k in sys.modules.keys() if k.startswith("easyeda2kicad")]
    for mod in modules_to_remove:
        del sys.modules[mod]

    # Add git submodule path at the very beginning
    easyeda_str = str(easyeda_submodule)
    if easyeda_str not in sys.path:
        sys.path.insert(0, easyeda_str)

    logger.info(f"Using easyeda2kicad from: {easyeda_submodule}")

from easyeda2kicad.easyeda.easyeda_importer import (  # noqa: E402
    Easyeda3dModelImporter,
    EasyedaFootprintImporter,
    EasyedaSymbolImporter,
)
from easyeda2kicad.easyeda.parameters_easyeda import EeFootprint  # noqa: E402
from easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad  # noqa: E402
from easyeda2kicad.kicad.export_kicad_footprint import (  # noqa: E402
    ExporterFootprintKicad,
)
from easyeda2kicad.kicad.export_kicad_symbol import ExporterSymbolKicad  # noqa: E402
from lcsc_download_cache import CachedEasyedaApi as EasyedaApi  # noqa: E402
from lcsc_model_download import is_step_data  # noqa: E402

logger.info("Successfully imported easyeda2kicad modules")


class ImportPaths(NamedTuple):
    """Container for all generated file paths"""

    symbol_lib: Path | None = None
    footprint_file: Path | None = None
    model_wrl: Path | None = None
    model_step: Path | None = None
    model_status: str = "unavailable"


@dataclass
class ImportConfig:
    """Configuration for import operations"""

    base_folder: Path
    lib_name: str = "EasyEDA"
    overwrite: bool = False
    lib_var: str = "${EASYEDA2KICAD}"
    prefer_step: bool = False
    compress_models: bool = False


def _recognizable_step(data: Any) -> bool:
    """Reject error pages and truncated responses before registering a STEP asset."""
    return is_step_data(data)


class EasyEDAImporter:
    """EasyEDA to KiCad component importer"""

    def __init__(self, config: ImportConfig, print_func: Callable[[str], None] | None = None):
        self.print_func = print_func or (lambda _: None)
        self.config = config
        self.config.base_folder = Path(self.config.base_folder).expanduser()
        self.api = EasyedaApi(print_func=self._print)

        self.symbol_lib_path = self.config.base_folder / f"{self.config.lib_name}.kicad_sym"
        self.footprint_dir = self.config.base_folder / f"{self.config.lib_name}.pretty"
        self.model_dir = self.config.base_folder / f"{self.config.lib_name}.3dshapes"
        self._model_status = "unavailable"

    def _print(self, message: str) -> None:
        self.print_func(message)

    def _ensure_directories(self) -> None:
        self.config.base_folder.mkdir(parents=True, exist_ok=True)
        self.footprint_dir.mkdir(exist_ok=True)
        self.model_dir.mkdir(exist_ok=True)

    def _import_symbol(self, cad_data: dict[str, Any]) -> tuple[bool, str | None]:
        """Import symbol and return success status and component name."""
        try:
            easyeda_symbol = EasyedaSymbolImporter(easyeda_cp_cad_data=cad_data).get_symbol()
            component_name = easyeda_symbol.info.name

            exporter = ExporterSymbolKicad(
                symbol=easyeda_symbol,
                lib_path=str(self.symbol_lib_path),
            )
            if not exporter.save_to_lib(
                lib_path=str(self.symbol_lib_path),
                footprint_lib_name=self.config.lib_name,
                overwrite=self.config.overwrite,
            ):
                self._print(f"Symbol '{component_name}' already exists.")
                return False, component_name

            if easyeda_symbol.sub_symbols:
                self._print(
                    f"Imported {len(easyeda_symbol.sub_symbols)} sub-symbols for: {component_name}"
                )
            self._print(f"Saved symbol: {component_name}")
            return True, component_name

        except Exception as e:
            self._print(f"Failed to import symbol: {e}")
            logger.error(f"Symbol import failed: {e}")
            return False, None

    def _import_footprint(self, ee_footprint: EeFootprint, model_ext: str = "wrl") -> Path | None:
        """Export footprint to .kicad_mod and return the file path."""
        try:
            footprint_file = self.footprint_dir / f"{ee_footprint.info.name}.kicad_mod"

            if footprint_file.exists() and not self.config.overwrite:
                self._print(f"Footprint already exists: {footprint_file.name}")
                return None

            model_3d_path = f"{self.config.lib_var}/{self.config.lib_name}.3dshapes"
            ExporterFootprintKicad(footprint=ee_footprint).export(
                footprint_full_path=str(footprint_file),
                model_3d_path=model_3d_path,
                model_3d_extension=model_ext,
            )
            self._print(f"Created footprint: {footprint_file.name}")
            return footprint_file

        except Exception as e:
            self._print(f"Failed to import footprint: {e}")
            logger.error(f"Footprint import failed: {e}")
            return None

    def _import_3d_model(self, cad_data: dict[str, Any]) -> tuple[Path | None, Path | None]:
        """Download and export 3D model. Returns (wrl_path, step_path).

        When compress_models is True, STEP is saved as .step.gz (gzip),
        natively supported by KiCad >= 6.0. WRL is not compressed.
        """
        self._model_status = "failed"
        try:
            model_3d = Easyeda3dModelImporter(
                easyeda_cp_cad_data=cad_data,
                download_raw_3d_model=False,
                api=self.api,
            ).output

            if model_3d is None:
                self._model_status = "unavailable"
                self._print("此器件未提供 3D 模型，继续导入符号与封装。")
                return None, None
        except Exception as e:
            self._print(f"3D 模型信息解析失败：{e}")
            logger.error(f"3D model metadata failed: {e}")
            return None, None

        started = perf_counter()
        self._print("正在并行下载 OBJ 与 STEP 3D 模型…")

        def download(method):
            requested = perf_counter()
            try:
                return method(uuid=model_3d.uuid), None, perf_counter() - requested
            except Exception as error:
                # One failed resource must not discard the other model format.
                return None, error, perf_counter() - requested

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lcsc-model") as executor:
            downloads = {
                executor.submit(download, self.api.get_raw_3d_model_obj): ("OBJ", "raw_obj"),
                executor.submit(download, self.api.get_step_3d_model): ("STEP", "step"),
            }
            for future in as_completed(downloads):
                label, attribute = downloads[future]
                data, error, elapsed = future.result()
                if attribute == "step" and data and not _recognizable_step(data):
                    data = None
                    error = ValueError("服务器返回的内容不是完整的 STEP 模型。")
                setattr(model_3d, attribute, data)
                if error is not None:
                    self._print(f"{label} 模型下载失败（{elapsed:.1f} 秒）：{error}")
                    logger.warning("%s model download failed: %s", label, error)
                elif data:
                    self._print(f"{label} 模型下载完成（{elapsed:.1f} 秒）。")
                else:
                    self._print(f"未获取到 {label} 模型（{elapsed:.1f} 秒）。")

        self._print(f"3D 下载阶段完成（{perf_counter() - started:.1f} 秒），正在保存可用模型…")
        wrl: Path | None = None
        step: Path | None = None
        # The upstream exporter requires OBJ even to save STEP. Save each format
        # independently so a valid STEP survives an OBJ download/conversion error.
        if model_3d.raw_obj:
            try:
                exporter = Exporter3dModelKicad(model_3d=model_3d)
                if exporter.output and exporter.output.raw_wrl:
                    self.model_dir.mkdir(parents=True, exist_ok=True)
                    wrl_path = self.model_dir / f"{model_3d.name}.wrl"
                    if self.config.overwrite or not wrl_path.exists():
                        wrl_path.write_text(exporter.output.raw_wrl, encoding="utf-8")
                    wrl = wrl_path
                    self._print(f"已保存 WRL 模型：{wrl.name}")
            except Exception as error:
                self._print(f"WRL 模型转换或保存失败：{error}")
                logger.warning("WRL conversion failed: %s", error)
        if model_3d.step:
            try:
                self.model_dir.mkdir(parents=True, exist_ok=True)
                extension = "step.gz" if self.config.compress_models else "step"
                step_path = self.model_dir / f"{model_3d.name}.{extension}"
                if self.config.overwrite or not step_path.exists():
                    if self.config.compress_models:
                        with gzip.open(step_path, "wb", compresslevel=9) as stream:
                            stream.write(model_3d.step)
                    else:
                        step_path.write_bytes(model_3d.step)
                step = step_path
                self._print(f"已保存 STEP 模型：{step.name}")
            except Exception as error:
                self._print(f"STEP 模型保存失败：{error}")
                logger.warning("STEP save failed: %s", error)
        if not wrl and not step:
            self._print("未获取到可用 3D 模型，继续导入符号与封装。")
        else:
            self._model_status = "available"
        return wrl, step

    def import_component(self, component_id: str) -> ImportPaths:
        """
        Import a component and all its assets.
        Returns ImportPaths with all created file paths.
        """
        started = perf_counter()
        self._print(f"开始导入 {component_id}。")

        if not component_id.startswith("C"):
            error_msg = (
                f"Invalid component ID: '{component_id}' (must start with 'C', e.g., 'C2040')"
            )
            self._print(error_msg)
            raise ValueError(error_msg)

        self._ensure_directories()

        try:
            phase = perf_counter()
            self._print(f"正在下载 {component_id} 的符号与封装数据…")
            cad_data = self.api.get_cad_data_of_component(lcsc_id=component_id)
            if not cad_data:
                error_msg = f"Failed to fetch CAD data for component {component_id}"
                self._print(error_msg)
                raise RuntimeError(error_msg)
            self._print(f"符号与封装数据下载完成（{perf_counter() - phase:.1f} 秒）。")

            # Parse footprint for footprint export
            self._print("正在转换符号与封装…")
            ee_footprint = EasyedaFootprintImporter(easyeda_cp_cad_data=cad_data).get_footprint()

            symbol_ok, _ = self._import_symbol(cad_data)
            wrl_path, step_path = self._import_3d_model(cad_data)
            use_step = step_path is not None and (self.config.prefer_step or wrl_path is None)
            if use_step and step_path:
                model_ext = "step.gz" if self.config.compress_models else "step"
            else:
                model_ext = "wrl"
            self._print("正在保存封装与 3D 模型引用…")
            footprint_path = self._import_footprint(ee_footprint, model_ext=model_ext)

            result = ImportPaths(
                symbol_lib=self.symbol_lib_path if symbol_ok else None,
                footprint_file=footprint_path,
                model_wrl=wrl_path,
                model_step=step_path,
                model_status="available" if wrl_path or step_path else self._model_status,
            )

            created_files = sum(1 for path in result[:4] if path)
            if created_files > 0:
                self._print(
                    f"器件文件已生成（{perf_counter() - started:.1f} 秒，共 {created_files} 个文件）。"
                )
            else:
                self._print("转换结束，未生成新的器件文件。")

            return result

        except Exception as e:
            self._print(f"器件转换失败（{perf_counter() - started:.1f} 秒）：{e}")
            logger.error(f"Component import failed for {component_id}: {e}")
            raise


def import_easyeda_component(
    component_id: str, config: ImportConfig, print_func: Callable[[str], None]
) -> ImportPaths:
    importer = EasyEDAImporter(config, print_func)
    return importer.import_component(component_id)


# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    config = ImportConfig(
        base_folder=Path("~/Documents/Kicad/EasyEDA"),
        lib_name="EasyEDA",
        overwrite=False,
    )

    try:
        paths = import_easyeda_component("C2040", config, print)

        if paths.symbol_lib:
            print(f"Symbol was written to library: {paths.symbol_lib}")
        if paths.footprint_file:
            print(f"Footprint was created at: {paths.footprint_file}")
        if paths.model_wrl or paths.model_step:
            print("3D models were created:")
            if paths.model_wrl:
                print(f"  WRL: {paths.model_wrl}")
            if paths.model_step:
                print(f"  STEP: {paths.model_step}")

    except Exception as e:
        print(f"Import failed: {e}")
