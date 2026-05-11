from __future__ import annotations

import argparse
import logging
from pathlib import Path

from smart_finqa.config import AppConfig
from smart_finqa.logger import setup_logger
from smart_finqa.pipeline import PipelinePaths, SmartFinancePipeline


def _str2bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="财报智能问答Agent 系统")
    parser.add_argument("--base-dir", default=".", help="项目根目录，默认当前目录")
    parser.add_argument("--mode", default="all", choices=["ingest", "task2", "task3", "all"], help="运行模式")
    parser.add_argument("--full-data", default=False, type=_str2bool, help="是否优先使用全量数据目录")
    parser.add_argument("--workers", default=4, type=int, help="入库并发worker数量")
    parser.add_argument("--incremental", default=True, type=_str2bool, help="是否启用增量入库")
    parser.add_argument("--config", default=None, help="配置文件路径（YAML格式）")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别")
    parser.add_argument("--enable-cache", default=True, type=_str2bool, help="是否启用查询缓存")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve()

    # Load configuration
    config_path = Path(args.config) if args.config else None
    env_cfg = AppConfig.from_file_or_env(config_path)

    # Override with command line arguments
    env_cfg.mode = args.mode
    env_cfg.full_data = bool(args.full_data)
    env_cfg.ingest_workers = max(1, int(args.workers))
    env_cfg.incremental_ingest = bool(args.incremental)
    env_cfg.log_level = args.log_level
    env_cfg.enable_cache = bool(args.enable_cache)

    # Setup logging
    log_file = Path(env_cfg.log_file) if env_cfg.log_file else base_dir / "outputs" / "smart_finqa.log"
    log_level = getattr(logging, env_cfg.log_level, logging.INFO)
    logger = setup_logger(
        name="smart_finqa",
        log_file=log_file,
        level=log_level,
        console=True,
    )

    logger.info("=" * 80)
    logger.info("Smart FinQA Pipeline Starting")
    logger.info(f"Mode: {env_cfg.mode} | Workers: {env_cfg.ingest_workers} | Cache: {env_cfg.enable_cache}")
    logger.info(f"Full Data: {env_cfg.full_data} | Incremental: {env_cfg.incremental_ingest}")
    logger.info("=" * 80)

    paths = PipelinePaths.from_base_dir(base_dir, full_data=env_cfg.full_data)
    pipeline = SmartFinancePipeline(paths, app_config=env_cfg)

    try:
        outputs = pipeline.run(mode=env_cfg.mode)

        logger.info("=" * 80)
        logger.info("Pipeline finished successfully:")
        for key, value in outputs.items():
            logger.info(f"  - {key}: {value}")

        # Print statistics
        if hasattr(pipeline.db, 'get_cache_stats'):
            cache_stats = pipeline.db.get_cache_stats()
            logger.info(f"Cache Stats: {cache_stats}")

        if hasattr(pipeline.llm_client, 'get_stats'):
            llm_stats = pipeline.llm_client.get_stats()
            logger.info(f"LLM Stats: {llm_stats}")

        logger.info("=" * 80)

    except Exception as exc:
        logger.error(f"Pipeline failed with error: {exc}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
