from __future__ import annotations

import argparse
import logging
from pathlib import Path

from smart_finqa.config import AppConfig
from smart_finqa.logger import setup_logger
from smart_finqa.pipeline import PipelinePaths, SmartFinancePipeline


def _str2bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"无效的布尔值: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="财报智能问答 Agent 系统")
    parser.add_argument("--base-dir", default=".", help="项目根目录，默认当前目录")
    parser.add_argument("--mode", choices=["ingest", "task2", "task3", "all"], help="运行模式")
    parser.add_argument("--full-data", type=_str2bool, help="是否优先使用全量数据目录")
    parser.add_argument("--workers", type=int, help="入库并发 worker 数量")
    parser.add_argument("--incremental", type=_str2bool, help="是否启用增量入库")
    parser.add_argument("--config", help="配置文件路径（YAML 格式）")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别")
    parser.add_argument("--enable-cache", type=_str2bool, help="是否启用查询缓存")
    return parser


def _apply_cli_overrides(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    if args.mode is not None:
        config.mode = args.mode
    if args.full_data is not None:
        config.full_data = args.full_data
    if args.workers is not None:
        if args.workers < 1:
            raise ValueError("--workers 必须大于或等于 1")
        config.ingest_workers = args.workers
    if args.incremental is not None:
        config.incremental_ingest = args.incremental
    if args.log_level is not None:
        config.log_level = args.log_level
    if args.enable_cache is not None:
        config.enable_cache = args.enable_cache
    return config


def main() -> None:
    args = build_parser().parse_args()
    base_dir = Path(args.base_dir).resolve()
    config_path = Path(args.config).resolve() if args.config else None
    config = _apply_cli_overrides(AppConfig.from_file_or_env(config_path), args)

    log_file = Path(config.log_file) if config.log_file else base_dir / "outputs" / "smart_finqa.log"
    logger = setup_logger(
        name="smart_finqa",
        log_file=log_file,
        level=getattr(logging, config.log_level),
        console=True,
    )

    logger.info("=" * 80)
    logger.info("Smart FinQA Pipeline Starting")
    logger.info(f"Mode: {config.mode} | Workers: {config.ingest_workers} | Cache: {config.enable_cache}")
    logger.info(f"Full Data: {config.full_data} | Incremental: {config.incremental_ingest}")
    logger.info("=" * 80)

    pipeline = SmartFinancePipeline(
        PipelinePaths.from_base_dir(base_dir, full_data=config.full_data),
        app_config=config,
    )
    try:
        outputs = pipeline.run(mode=config.mode)
    except Exception:
        logger.exception("Pipeline failed")
        raise

    logger.info("=" * 80)
    logger.info("Pipeline finished successfully:")
    for key, value in outputs.items():
        logger.info(f"  - {key}: {value}")
    logger.info(f"Cache Stats: {pipeline.db.get_cache_stats()}")
    logger.info(f"LLM Stats: {pipeline.llm_client.get_stats()}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
