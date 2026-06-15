# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2024 funstory.ai limited
# Copyright (C) 2026 JiajunDeng
#
# Derived from BabelDOC commit 980fd2821d54cbabd270349fe509e8177c35e4c3.
# Modified on 2026-06-09 to support explicit local runtime asset directories
# and a separate asset download preparation script for the pdf-translate skill.

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from babeldoc.assets import embedding_assets_metadata
from babeldoc.assets.embedding_assets_metadata import CMAP_METADATA
from babeldoc.assets.embedding_assets_metadata import CMAP_URL_BY_UPSTREAM
from babeldoc.assets.embedding_assets_metadata import DOC_LAYOUT_ONNX_MODEL_URL
from babeldoc.assets.embedding_assets_metadata import (
    DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256,
)
from babeldoc.assets.embedding_assets_metadata import EMBEDDING_FONT_METADATA
from babeldoc.assets.embedding_assets_metadata import FONT_METADATA_URL
from babeldoc.assets.embedding_assets_metadata import FONT_URL_BY_UPSTREAM
from babeldoc.assets.embedding_assets_metadata import TIKTOKEN_CACHES
from babeldoc.const import TIKTOKEN_CACHE_FOLDER
from babeldoc.const import get_cache_file_path
from tenacity import retry
from tenacity import stop_after_attempt
from tenacity import wait_exponential

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


_FASTEST_FONT_UPSTREAM_LOCK = asyncio.Lock()
_FASTEST_FONT_UPSTREAM: str | None = None
_FASTEST_FONT_METADATA: dict | None = None
_RUNTIME_ASSET_DIR: Path | None = None
ASSET_MANIFEST_NAME = "manifest.json"
ASSET_GROUPS = ("models", "fonts", "cmap", "tiktoken")


class AssetError(RuntimeError):
    pass


def _load_httpx():
    try:
        import httpx
    except ImportError as exc:
        raise AssetError("httpx is required to download runtime assets") from exc
    return httpx


def _network_error_types() -> tuple[type[BaseException], ...]:
    try:
        import httpx
    except ImportError:
        return (ConnectionError, ValueError, TimeoutError)
    return (httpx.HTTPError, ConnectionError, ValueError, TimeoutError)


class ResultContainer:
    def __init__(self):
        self.result = None
        self.exception: BaseException | None = None

    def set_result(self, result):
        self.result = result

    def set_exception(self, exc: BaseException):
        self.exception = exc


def run_in_another_thread(coro):
    result_container = ResultContainer()

    def _wrapper():
        try:
            result_container.set_result(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001
            result_container.set_exception(exc)

    thread = threading.Thread(target=_wrapper)
    thread.start()
    thread.join()
    if result_container.exception is not None:
        msg = (
            "asset coroutine failed: "
            f"{type(result_container.exception).__name__}: {result_container.exception}"
        )
        raise RuntimeError(msg) from result_container.exception
    return result_container.result


def run_coro(coro):
    return run_in_another_thread(coro)


def _retry_if_not_cancelled_and_failed(retry_state):
    """Only retry if the exception is not CancelledError and the attempt failed."""
    if retry_state.outcome.failed:
        exception = retry_state.outcome.exception()
        # Don't retry on CancelledError
        if isinstance(exception, asyncio.CancelledError):
            logger.debug("Operation was cancelled, not retrying")
            return False
        # Retry on network related errors
        if isinstance(exception, _network_error_types()):
            logger.warning(f"Network error occurred: {exception}, will retry")
            return True
    # Don't retry on success
    return False


def verify_file(path: Path, sha3_256: str):
    if not path.exists():
        return False
    hash_ = hashlib.sha3_256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            hash_.update(chunk)
    return hash_.hexdigest() == sha3_256


def _asset_path(file_type: str, filename: str) -> Path:
    if _RUNTIME_ASSET_DIR is not None:
        return _RUNTIME_ASSET_DIR / file_type / filename
    return get_cache_file_path(filename, file_type)


def _require_runtime_asset(file_type: str, filename: str, sha3_256: str) -> Path:
    path = _asset_path(file_type, filename)
    if verify_file(path, sha3_256):
        return path
    raise AssetError(f"asset file missing or hash mismatch: {path}")


def _normalize_manifest(manifest: dict) -> dict:
    normalized: dict[str, list[dict[str, str]]] = {}
    for group in ASSET_GROUPS:
        items = manifest.get(group)
        if not isinstance(items, list):
            raise AssetError(f"asset manifest must contain a {group} list")
        normalized[group] = sorted(
            (
                {
                    "name": str(item.get("name")),
                    "sha3_256": str(item.get("sha3_256")),
                }
                for item in items
                if isinstance(item, dict)
            ),
            key=lambda item: item["name"],
        )
        if len(normalized[group]) != len(items):
            raise AssetError(f"asset manifest has invalid entries in {group}")
    return normalized


def validate_runtime_asset_dir(asset_dir: str | Path) -> Path:
    root = Path(asset_dir).expanduser().resolve()
    if not root.is_dir():
        raise AssetError(f"asset_dir does not exist or is not a directory: {root}")

    manifest_path = root / ASSET_MANIFEST_NAME
    if not manifest_path.is_file():
        raise AssetError(f"asset manifest is required: {manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AssetError(f"cannot read asset manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise AssetError("asset manifest must contain a mapping")

    expected = _normalize_manifest(generate_all_assets_file_list())
    current = _normalize_manifest(manifest)
    if current != expected:
        raise AssetError("asset manifest does not match the expected asset list")

    for group, items in expected.items():
        for item in items:
            path = root / group / item["name"]
            if not verify_file(path, item["sha3_256"]):
                raise AssetError(f"asset file missing or hash mismatch: {path}")
    return root


def set_runtime_asset_dir(asset_dir: str | Path) -> Path:
    global _RUNTIME_ASSET_DIR
    root = validate_runtime_asset_dir(asset_dir)
    _RUNTIME_ASSET_DIR = root
    os.environ["TIKTOKEN_CACHE_DIR"] = str(root / "tiktoken")
    return root


def clear_runtime_asset_dir() -> None:
    global _RUNTIME_ASSET_DIR
    _RUNTIME_ASSET_DIR = None
    os.environ["TIKTOKEN_CACHE_DIR"] = str(TIKTOKEN_CACHE_FOLDER)


@retry(
    retry=_retry_if_not_cancelled_and_failed,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    before_sleep=lambda retry_state: logger.warning(
        f"Download file failed, retrying in {retry_state.next_action.sleep} seconds... "
        f"(Attempt {retry_state.attempt_number}/3)"
    ),
)
async def download_file(
    client: httpx.AsyncClient | None = None,
    url: str = None,
    path: Path = None,
    sha3_256: str = None,
):
    httpx_module = _load_httpx()
    path.parent.mkdir(parents=True, exist_ok=True)
    if client is None:
        async with httpx_module.AsyncClient(timeout=60.0) as client:
            response = await client.get(url, follow_redirects=True)
    else:
        response = await client.get(url, follow_redirects=True)

    response.raise_for_status()
    with path.open("wb") as f:
        f.write(response.content)
    if not verify_file(path, sha3_256):
        path.unlink(missing_ok=True)
        raise ValueError(f"File {path} is corrupted")


@retry(
    retry=_retry_if_not_cancelled_and_failed,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    before_sleep=lambda retry_state: logger.warning(
        f"Get font metadata failed, retrying in {retry_state.next_action.sleep} seconds... "
        f"(Attempt {retry_state.attempt_number}/3)"
    ),
)
async def get_font_metadata(
    client: httpx.AsyncClient | None = None, upstream: str = None
):
    httpx_module = _load_httpx()
    if upstream not in FONT_METADATA_URL:
        logger.critical(f"Invalid upstream: {upstream}")
        exit(1)

    if client is None:
        async with httpx_module.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                FONT_METADATA_URL[upstream], follow_redirects=True
            )
    else:
        response = await client.get(FONT_METADATA_URL[upstream], follow_redirects=True)

    response.raise_for_status()
    logger.debug(f"Get font metadata from {upstream} success")
    return upstream, response.json()


async def _get_fastest_upstream_for_font_internal(
    client: httpx.AsyncClient | None = None, exclude_upstream: list[str] | None = None
) -> tuple[str | None, dict | None]:
    """Find the fastest upstream for font metadata without using cached result."""
    tasks: list[asyncio.Task[tuple[str, dict]]] = []
    for upstream in FONT_METADATA_URL:
        if exclude_upstream and upstream in exclude_upstream:
            continue
        tasks.append(asyncio.create_task(get_font_metadata(client, upstream)))
    for future in asyncio.as_completed(tasks):
        try:
            result = await future
            for task in tasks:
                if not task.done():
                    task.cancel()
            return result
        except Exception as e:
            logger.exception(f"Error getting font metadata: {e}")
    logger.error("All upstreams failed")
    return None, None


async def get_fastest_upstream_for_font(
    client: httpx.AsyncClient | None = None, exclude_upstream: list[str] | None = None
) -> tuple[str | None, dict | None]:
    """Get the fastest upstream for font metadata with cached result.

    The cached upstream is only used when exclude_upstream is None.
    """
    global _FASTEST_FONT_UPSTREAM, _FASTEST_FONT_METADATA

    if exclude_upstream is None and _FASTEST_FONT_UPSTREAM is not None:
        return _FASTEST_FONT_UPSTREAM, _FASTEST_FONT_METADATA

    if exclude_upstream is not None:
        # Do not use or update cache when exclude_upstream is provided.
        return await _get_fastest_upstream_for_font_internal(client, exclude_upstream)

    async with _FASTEST_FONT_UPSTREAM_LOCK:
        if _FASTEST_FONT_UPSTREAM is not None:
            return _FASTEST_FONT_UPSTREAM, _FASTEST_FONT_METADATA

        upstream, metadata = await _get_fastest_upstream_for_font_internal(client)
        if upstream is not None:
            _FASTEST_FONT_UPSTREAM = upstream
            _FASTEST_FONT_METADATA = metadata
            logger.info(f"Fastest font upstream determined: {upstream}")
        return upstream, metadata


async def get_fastest_upstream_for_model(client: httpx.AsyncClient | None = None):
    return await get_fastest_upstream_for_font(client, exclude_upstream=["github"])




async def get_doclayout_onnx_model_path_async(client: httpx.AsyncClient | None = None):
    model_name = "doclayout_yolo_docstructbench_imgsz1024.onnx"
    if _RUNTIME_ASSET_DIR is not None:
        return _require_runtime_asset(
            "models", model_name, DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256
        )

    onnx_path = get_cache_file_path(
        model_name, "models"
    )
    if verify_file(onnx_path, DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256):
        return onnx_path

    logger.info("doclayout onnx model not found or corrupted, downloading...")
    fastest_upstream, _ = await get_fastest_upstream_for_model(client)
    if fastest_upstream is None:
        logger.error("Failed to get fastest upstream")
        exit(1)

    url = DOC_LAYOUT_ONNX_MODEL_URL[fastest_upstream]

    await download_file(
        client, url, onnx_path, DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256
    )
    logger.info(f"Download doclayout onnx model from {fastest_upstream} success")
    return onnx_path


def get_doclayout_onnx_model_path():
    return run_coro(get_doclayout_onnx_model_path_async())


def get_font_url_by_name_and_upstream(font_file_name: str, upstream: str):
    if upstream not in FONT_URL_BY_UPSTREAM:
        logger.critical(f"Invalid upstream: {upstream}")
        exit(1)

    return FONT_URL_BY_UPSTREAM[upstream](font_file_name)


async def get_font_and_metadata_async(
    font_file_name: str,
    client: httpx.AsyncClient | None = None,
    fastest_upstream: str | None = None,
    font_metadata: dict | None = None,
):
    if _RUNTIME_ASSET_DIR is not None:
        if font_file_name not in EMBEDDING_FONT_METADATA:
            raise AssetError(f"font asset is not listed: {font_file_name}")
        metadata = EMBEDDING_FONT_METADATA[font_file_name]
        return (
            _require_runtime_asset(
                "fonts", font_file_name, metadata["sha3_256"]
            ),
            metadata,
        )

    cache_file_path = get_cache_file_path(font_file_name, "fonts")
    if font_file_name in EMBEDDING_FONT_METADATA and verify_file(
        cache_file_path, EMBEDDING_FONT_METADATA[font_file_name]["sha3_256"]
    ):
        return cache_file_path, EMBEDDING_FONT_METADATA[font_file_name]

    logger.info(f"Font {cache_file_path} not found or corrupted, downloading...")
    if fastest_upstream is None:
        fastest_upstream, font_metadata = await get_fastest_upstream_for_font(client)
        if fastest_upstream is None:
            logger.critical("Failed to get fastest upstream")
            exit(1)

        if font_file_name not in font_metadata:
            logger.critical(f"Font {font_file_name} not found in {font_metadata}")
            exit(1)

        if verify_file(cache_file_path, font_metadata[font_file_name]["sha3_256"]):
            return cache_file_path, font_metadata[font_file_name]

    assert font_metadata is not None
    logger.info(f"download {font_file_name} from {fastest_upstream}")

    url = get_font_url_by_name_and_upstream(font_file_name, fastest_upstream)
    if "sha3_256" not in font_metadata[font_file_name]:
        logger.critical(f"Font {font_file_name} not found in {font_metadata}")
        exit(1)
    await download_file(
        client, url, cache_file_path, font_metadata[font_file_name]["sha3_256"]
    )
    return cache_file_path, font_metadata[font_file_name]


def get_font_and_metadata(font_file_name: str):
    return run_coro(get_font_and_metadata_async(font_file_name))


async def get_cmap_file_path_async(
    name: str, client: httpx.AsyncClient | None = None
) -> Path:
    """Get cached cmap file path, downloading it if necessary."""
    if name.endswith(".json"):
        file_name = name
    else:
        file_name = f"{name}.json"

    if file_name not in CMAP_METADATA:
        logger.critical(f"CMap {file_name} not found in CMAP_METADATA")
        exit(1)

    meta = CMAP_METADATA[file_name]
    if _RUNTIME_ASSET_DIR is not None:
        return _require_runtime_asset("cmap", file_name, meta["sha3_256"])

    cache_file_path = get_cache_file_path(file_name, "cmap")
    if verify_file(cache_file_path, meta["sha3_256"]):
        return cache_file_path

    logger.info(f"CMap {cache_file_path} not found or corrupted, downloading...")
    await download_cmap_file_async(file_name, client)
    if not verify_file(cache_file_path, meta["sha3_256"]):
        logger.critical(f"Failed to verify downloaded cmap file: {cache_file_path}")
        exit(1)
    return cache_file_path


async def download_cmap_file_async(
    file_name: str, client: httpx.AsyncClient | None = None
) -> Path:
    """Download a single cmap file to cache directory."""
    if file_name not in CMAP_METADATA:
        logger.critical(f"CMap {file_name} not found in CMAP_METADATA")
        exit(1)

    fastest_upstream, _ = await get_fastest_upstream_for_font(client)
    if fastest_upstream is None:
        logger.critical("Failed to get fastest upstream for cmap")
        exit(1)

    if fastest_upstream not in CMAP_URL_BY_UPSTREAM:
        logger.critical(f"Invalid fastest upstream for cmap: {fastest_upstream}")
        exit(1)

    url = CMAP_URL_BY_UPSTREAM[fastest_upstream](file_name)
    cache_file_path = get_cache_file_path(file_name, "cmap")
    sha3_256 = CMAP_METADATA[file_name]["sha3_256"]
    await download_file(client, url, cache_file_path, sha3_256)
    return cache_file_path


async def get_cmap_data_async(
    name: str, client: httpx.AsyncClient | None = None
) -> dict:
    """Load cmap json data from cached file, downloading it if necessary."""
    path = await get_cmap_file_path_async(name, client)
    return json.loads(path.read_text())




def get_cmap_data(name: str):
    return run_coro(get_cmap_data_async(name))


def get_font_family(lang_code: str):
    font_family = embedding_assets_metadata.get_font_family(lang_code)
    return font_family


async def download_all_fonts_async(client: httpx.AsyncClient | None = None):
    for font_file_name in EMBEDDING_FONT_METADATA:
        if not verify_file(
            get_cache_file_path(font_file_name, "fonts"),
            EMBEDDING_FONT_METADATA[font_file_name]["sha3_256"],
        ):
            break
    else:
        logger.debug("All fonts are already downloaded")
        return

    fastest_upstream, font_metadata = await get_fastest_upstream_for_font(client)
    if fastest_upstream is None:
        logger.error("Failed to get fastest upstream")
        exit(1)
    logger.info(f"Downloading fonts from {fastest_upstream}")

    font_tasks = [
        asyncio.create_task(
            get_font_and_metadata_async(
                font_file_name, client, fastest_upstream, font_metadata
            )
        )
        for font_file_name in EMBEDDING_FONT_METADATA
    ]
    await asyncio.gather(*font_tasks)


async def download_all_cmaps_async(client: httpx.AsyncClient | None = None):
    """Download all cmap files defined in CMAP_METADATA."""
    for cmap_file_name, meta in CMAP_METADATA.items():
        if not verify_file(
            get_cache_file_path(cmap_file_name, "cmap"),
            meta["sha3_256"],
        ):
            break
    else:
        logger.debug("All cmaps are already downloaded")
        return

    fastest_upstream, _ = await get_fastest_upstream_for_font(client)
    if fastest_upstream is None:
        logger.error("Failed to get fastest upstream for cmap")
        exit(1)
    logger.info(f"Downloading cmaps from {fastest_upstream}")

    cmap_tasks = [
        asyncio.create_task(get_cmap_file_path_async(cmap_file_name, client))
        for cmap_file_name in CMAP_METADATA
    ]
    await asyncio.gather(*cmap_tasks)






def generate_all_assets_file_list():
    result: dict[str, list[dict[str, str]]] = {}
    result["fonts"] = []
    result["models"] = []
    result["tiktoken"] = []
    result["cmap"] = []
    for font_file_name in EMBEDDING_FONT_METADATA:
        result["fonts"].append(
            {
                "name": font_file_name,
                "sha3_256": EMBEDDING_FONT_METADATA[font_file_name]["sha3_256"],
            }
        )
    for cmap_file_name in CMAP_METADATA:
        result["cmap"].append(
            {
                "name": cmap_file_name,
                "sha3_256": CMAP_METADATA[cmap_file_name]["sha3_256"],
            }
        )
    for tiktoken_file, sha3_256 in TIKTOKEN_CACHES.items():
        result["tiktoken"].append(
            {
                "name": tiktoken_file,
                "sha3_256": sha3_256,
            }
        )
    result["models"].append(
        {
            "name": "doclayout_yolo_docstructbench_imgsz1024.onnx",
            "sha3_256": DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256,
        },
    )
    return result


def _write_asset_manifest(asset_dir: Path) -> None:
    manifest = generate_all_assets_file_list()
    (asset_dir / ASSET_MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _prepare_tiktoken_assets(asset_dir: Path) -> None:
    tiktoken_dir = asset_dir / "tiktoken"
    tiktoken_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(tiktoken_dir)

    if _asset_group_is_complete(tiktoken_dir, TIKTOKEN_CACHES):
        return

    from tiktoken import encoding_for_model

    _ = encoding_for_model("gpt-4o")
    for file_name, sha3_256 in TIKTOKEN_CACHES.items():
        path = tiktoken_dir / file_name
        if not verify_file(path, sha3_256):
            raise AssetError(f"tiktoken asset missing or hash mismatch: {path}")


def _asset_group_is_complete(group_dir: Path, metadata: dict[str, dict | str]) -> bool:
    for file_name, item in metadata.items():
        sha3_256 = item["sha3_256"] if isinstance(item, dict) else item
        if not verify_file(group_dir / file_name, sha3_256):
            return False
    return True


async def _download_doclayout_model_to(
    asset_dir: Path, client: httpx.AsyncClient
) -> None:
    file_name = "doclayout_yolo_docstructbench_imgsz1024.onnx"
    target = asset_dir / "models" / file_name
    sha3_256 = DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256
    if verify_file(target, sha3_256):
        return

    fastest_upstream, _ = await get_fastest_upstream_for_model(client)
    if fastest_upstream is None:
        raise AssetError("failed to choose a model asset upstream")
    await download_file(
        client,
        DOC_LAYOUT_ONNX_MODEL_URL[fastest_upstream],
        target,
        sha3_256,
    )


async def _download_fonts_to(asset_dir: Path, client: httpx.AsyncClient) -> None:
    if _asset_group_is_complete(asset_dir / "fonts", EMBEDDING_FONT_METADATA):
        return

    fastest_upstream, _ = await get_fastest_upstream_for_font(client)
    if fastest_upstream is None:
        raise AssetError("failed to choose a font asset upstream")

    for file_name, metadata in EMBEDDING_FONT_METADATA.items():
        target = asset_dir / "fonts" / file_name
        sha3_256 = metadata["sha3_256"]
        if verify_file(target, sha3_256):
            continue
        url = get_font_url_by_name_and_upstream(file_name, fastest_upstream)
        await download_file(client, url, target, sha3_256)


async def _download_cmaps_to(asset_dir: Path, client: httpx.AsyncClient) -> None:
    if _asset_group_is_complete(asset_dir / "cmap", CMAP_METADATA):
        return

    fastest_upstream, _ = await get_fastest_upstream_for_font(client)
    if fastest_upstream is None:
        raise AssetError("failed to choose a CMap asset upstream")
    if fastest_upstream not in CMAP_URL_BY_UPSTREAM:
        raise AssetError(f"invalid CMap asset upstream: {fastest_upstream}")

    for file_name, metadata in CMAP_METADATA.items():
        target = asset_dir / "cmap" / file_name
        sha3_256 = metadata["sha3_256"]
        if verify_file(target, sha3_256):
            continue
        url = CMAP_URL_BY_UPSTREAM[fastest_upstream](file_name)
        await download_file(client, url, target, sha3_256)


async def download_runtime_assets_async(output_directory: Path) -> Path:
    asset_dir = Path(output_directory).expanduser().resolve()
    try:
        return validate_runtime_asset_dir(asset_dir)
    except AssetError:
        pass

    for group in ASSET_GROUPS:
        (asset_dir / group).mkdir(parents=True, exist_ok=True)

    httpx_module = _load_httpx()
    async with httpx_module.AsyncClient(timeout=60.0) as client:
        await _download_doclayout_model_to(asset_dir, client)
        await _download_fonts_to(asset_dir, client)
        await _download_cmaps_to(asset_dir, client)
    _prepare_tiktoken_assets(asset_dir)
    _write_asset_manifest(asset_dir)
    return validate_runtime_asset_dir(asset_dir)


def download_runtime_assets(output_directory: Path) -> Path:
    return run_coro(download_runtime_assets_async(output_directory))












if __name__ == "__main__":
    from rich.logging import RichHandler

    logging.basicConfig(level=logging.DEBUG, handlers=[RichHandler()])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
