from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


ACEBENCH_ROOT = Path(os.environ.get("ACEBENCH_ROOT", "/workspace/sber-ACEBench"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provider contract runner for sber-ACEBench.")
    parser.add_argument("--benchmark-name", default="sber_acebench")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--candidate-base-url", required=True)
    parser.add_argument("--candidate-model-id", required=True)
    parser.add_argument("--candidate-api-key", default="")
    parser.add_argument("--parallelism", type=int, default=1)
    parser.add_argument("--request-timeout", type=int, default=1200)
    parser.add_argument("--limit-samples", type=int, default=-1)
    parser.add_argument("--request-params-json", default="{}")
    parser.add_argument("--resume", default="1")
    parser.add_argument("--show-live-stats", default="1")
    return parser.parse_args()


def _to_bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _safe_json_obj(raw: str) -> dict[str, Any]:
    payload = json.loads(raw) if raw.strip() else {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _sanitize_metric_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower() or "metric"


def _normalize_categories(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.replace("\n", ",").split(",")]
        return [part for part in parts if part]
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, Mapping):
                rows.append(dict(payload))
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")


def _import_acebench_modules() -> tuple[Any, Any, dict[str, list[str]]]:
    if not ACEBENCH_ROOT.exists():
        raise FileNotFoundError(f"sber-ACEBench root is missing: {ACEBENCH_ROOT}")

    root_text = str(ACEBENCH_ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    import eval_main as acebench_eval_main
    import generate as acebench_generate
    from category import ACE_DATA_CATEGORY

    return acebench_generate, acebench_eval_main, ACE_DATA_CATEGORY


def _expand_categories(
    requested_categories: list[str],
    category_map: Mapping[str, list[str]],
) -> list[str]:
    leaf_categories = {leaf for values in category_map.values() for leaf in values}
    expanded: list[str] = []
    for category in requested_categories:
        if category in category_map:
            expanded.extend(category_map[category])
            continue
        if category in leaf_categories:
            expanded.append(category)
            continue
        supported = ", ".join(sorted(set(category_map.keys()) | set(leaf_categories)))
        raise ValueError(
            f"Unsupported ACEBench category '{category}'. Supported values: {supported}"
        )
    # Keep order while deduplicating.
    return list(dict.fromkeys(expanded))


def _serialize_response(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _collect_prompt_index(data_root: Path, categories: list[str]) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for category in categories:
        data_path = data_root / f"data_{category}.json"
        for row in _load_jsonl(data_path):
            sample_id = row.get("id")
            if sample_id is None:
                continue
            prompts[str(sample_id)] = str(row.get("question", ""))
    return prompts


def _collect_category_scores(
    *,
    score_root: Path,
    model_name: str,
    categories: list[str],
) -> dict[str, dict[str, Any]]:
    collected: dict[str, dict[str, Any]] = {}
    for category in categories:
        score_path = score_root / model_name / f"data_{category}_score.json"
        rows = _load_jsonl(score_path)
        if not rows:
            raise FileNotFoundError(f"Missing score output: {score_path}")

        header = rows[0]
        if "accuracy" in header:
            accuracy = _safe_float(header.get("accuracy"), default=0.0)
            total_count = _safe_int(header.get("total_count"), default=0)
            correct_count = _safe_int(
                header.get("correct_count"),
                default=round(accuracy * total_count),
            )
            process_accuracy = None
        else:
            accuracy = _safe_float(header.get("end_to_end_accuracy"), default=0.0)
            total_count = _safe_int(header.get("total_count"), default=0)
            correct_count = _safe_int(
                header.get("end_correct_count"),
                default=round(accuracy * total_count),
            )
            process_accuracy = _safe_float(header.get("process_accuracy"), default=0.0)

        failed_by_id: dict[str, str] = {}
        for row in rows[1:]:
            sample_id = row.get("id")
            if sample_id is None:
                continue
            error_value = row.get("error")
            if isinstance(error_value, list):
                error = "; ".join(str(item) for item in error_value)
            elif error_value is None:
                error = "evaluation_failed"
            else:
                error = str(error_value)
            failed_by_id[str(sample_id)] = error

        collected[category] = {
            "accuracy": accuracy,
            "process_accuracy": process_accuracy,
            "total_count": total_count,
            "correct_count": correct_count,
            "failed_by_id": failed_by_id,
            "score_path": str(score_path),
        }
    return collected


def _run_generation(
    *,
    acebench_generate: Any,
    data_root: Path,
    result_root: Path,
    model_name: str,
    categories: list[str],
    parallelism: int,
    temperature: float,
    top_p: float,
    max_tokens: int,
    max_dialog_turns: int,
    user_model: str,
    language: str,
    resume: bool,
) -> None:
    test_files = [f"data_{category}.json" for category in categories]
    test_cases = acebench_generate.load_test_cases(str(data_root), test_files)

    completed_ids: set[str] = set()
    if resume:
        for category in categories:
            result_path = result_root / model_name / f"data_{category}_result.json"
            for row in _load_jsonl(result_path):
                sample_id = row.get("id")
                if sample_id is not None:
                    completed_ids.add(str(sample_id))

    to_run = [case for case in test_cases if str(case.get("id")) not in completed_ids]
    generation_args = argparse.Namespace(
        model_path=None,
        result_path=f"{result_root}/",
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        max_dialog_turns=max_dialog_turns,
        user_model=user_model,
        language=language,
        num_threads=parallelism,
    )

    with ThreadPoolExecutor(max_workers=max(1, parallelism)) as executor:
        futures = [
            executor.submit(acebench_generate.generate_singal, generation_args, model_name, case)
            for case in to_run
        ]
        for future in as_completed(futures):
            future.result()

    # Multithreaded writes can be out of order; sort result files for deterministic eval.
    for category in categories:
        result_path = result_root / model_name / f"data_{category}_result.json"
        if result_path.exists():
            acebench_generate.sort_json(str(result_path))


def _run_scoring(
    *,
    acebench_eval_main: Any,
    paths: dict[str, str],
    model_name: str,
    categories: list[str],
) -> None:
    acebench_eval_main.RESULT_TABLE = {}
    acebench_eval_main.INPUT_PATH = paths["INPUT_PATH"]
    acebench_eval_main.PROMPT_PATH = paths["PROMPT_PATH"]
    acebench_eval_main.POSSIBLE_ANSWER_PATH = paths["POSSIBLE_ANSWER_PATH"]
    acebench_eval_main.OUTPUT_PATH = paths["OUTPUT_PATH"]
    acebench_eval_main.runner([model_name], categories, paths)


def main() -> int:
    args = _parse_args()
    started_at = datetime.now(UTC)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.limit_samples >= 0:
        raise ValueError(
            "sber_acebench does not support partial sampling. Keep limit_samples=-1 "
            "and reduce scope via acebench_categories."
        )

    request_params = _safe_json_obj(args.request_params_json)
    show_live_stats = _to_bool(args.show_live_stats)
    resume = _to_bool(args.resume)

    acebench_generate, acebench_eval_main, category_map = _import_acebench_modules()

    language = str(request_params.get("acebench_language", "en")).strip().lower() or "en"
    if language not in {"en", "zh"}:
        raise ValueError("acebench_language must be one of: en, zh")

    requested_categories = _normalize_categories(request_params.get("acebench_categories", "test_all"))
    if not requested_categories:
        requested_categories = ["test_all"]
    leaf_categories = _expand_categories(requested_categories, category_map)
    if not leaf_categories:
        raise ValueError("No ACEBench categories resolved from acebench_categories")

    model_fs_name = re.sub(r"[\\/]+", "_", args.candidate_model_id).strip() or "candidate_model"
    user_model = (
        str(request_params.get("acebench_user_model", "")).strip() or args.candidate_model_id
    )
    temperature = _safe_float(request_params.get("temperature"), default=0.7)
    top_p = _safe_float(request_params.get("top_p"), default=1.0)
    max_tokens = max(1, _safe_int(request_params.get("max_tokens"), default=1200))
    max_dialog_turns = max(
        1,
        _safe_int(request_params.get("acebench_max_dialog_turns"), default=40),
    )
    parallelism = max(1, int(args.parallelism))

    workspace = output_dir / "acebench_workspace"
    result_root = workspace / "result_all" / f"result_{language}"
    score_root = workspace / "score_all" / f"score_{language}"
    data_root = ACEBENCH_ROOT / "data_all" / f"data_{language}"
    if not data_root.exists():
        raise FileNotFoundError(f"ACEBench data directory is missing: {data_root}")

    os.environ["ACEBENCH_API_KEY"] = args.candidate_api_key
    os.environ["ACEBENCH_BASE_URL"] = args.candidate_base_url
    os.environ["ACEBENCH_MODEL_ID"] = args.candidate_model_id
    os.environ["ACEBENCH_USER_MODEL_ID"] = user_model
    os.environ["ACEBENCH_TRACE_DIR"] = str((output_dir / "traces").resolve())
    os.environ["ACEBENCH_EXTRA_KWARGS"] = json.dumps(
        _safe_json_mapping(request_params.get("acebench_extra_kwargs")),
        ensure_ascii=False,
    )
    os.environ["OPENAI_API_KEY"] = args.candidate_api_key
    os.environ["OPENAI_BASE_URL"] = args.candidate_base_url

    if show_live_stats:
        print(
            f"[{args.benchmark_name}] categories={leaf_categories} language={language} "
            f"model={args.candidate_model_id}",
            flush=True,
        )

    _run_generation(
        acebench_generate=acebench_generate,
        data_root=data_root,
        result_root=result_root,
        model_name=model_fs_name,
        categories=leaf_categories,
        parallelism=parallelism,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        max_dialog_turns=max_dialog_turns,
        user_model=user_model,
        language=language,
        resume=resume,
    )

    paths = {
        "INPUT_PATH": f"{result_root}/",
        "PROMPT_PATH": f"{data_root}/",
        "POSSIBLE_ANSWER_PATH": f"{data_root}/possible_answer/",
        "OUTPUT_PATH": f"{score_root}/",
    }
    _run_scoring(
        acebench_eval_main=acebench_eval_main,
        paths=paths,
        model_name=model_fs_name,
        categories=leaf_categories,
    )

    prompts_by_id = _collect_prompt_index(data_root=data_root, categories=leaf_categories)
    category_scores = _collect_category_scores(
        score_root=score_root,
        model_name=model_fs_name,
        categories=leaf_categories,
    )

    predictions: list[dict[str, Any]] = []
    sample_id = 0
    for category in leaf_categories:
        result_path = result_root / model_fs_name / f"data_{category}_result.json"
        rows = _load_jsonl(result_path)
        failed_by_id = category_scores[category]["failed_by_id"]
        for row in rows:
            test_id = str(row.get("id", ""))
            error = failed_by_id.get(test_id)
            passed = 0 if error else 1
            predictions.append(
                {
                    "sample_id": sample_id,
                    "prompt": prompts_by_id.get(test_id, ""),
                    "response": _serialize_response(row.get("result")),
                    "target": "",
                    "status": "error" if error else "scored",
                    "error": error,
                    "scores": {
                        "acebench_pass": passed,
                        f"acebench_{_sanitize_metric_name(category)}_pass": passed,
                    },
                    "metadata": {
                        "model_id": args.candidate_model_id,
                        "acebench_test_id": test_id,
                        "acebench_category": category,
                        "acebench_result_source": str(result_path),
                    },
                }
            )
            sample_id += 1

    weighted_total = 0
    weighted_correct = 0
    process_values: list[float] = []
    metric_values: dict[str, tuple[float, int]] = {}
    for category in leaf_categories:
        payload = category_scores[category]
        accuracy = _safe_float(payload.get("accuracy"), default=0.0)
        total_count = _safe_int(payload.get("total_count"), default=0)
        correct_count = _safe_int(payload.get("correct_count"), default=0)
        weighted_total += total_count
        weighted_correct += correct_count
        metric_values[f"acebench_{_sanitize_metric_name(category)}_accuracy"] = (
            accuracy,
            total_count,
        )
        process_accuracy = payload.get("process_accuracy")
        if process_accuracy is not None:
            process_values.append(_safe_float(process_accuracy, default=0.0))

    weighted_accuracy = (weighted_correct / weighted_total) if weighted_total else 0.0
    unweighted_accuracy = (
        sum(_safe_float(category_scores[cat]["accuracy"]) for cat in leaf_categories)
        / len(leaf_categories)
        if leaf_categories
        else 0.0
    )
    metric_values["acebench_weighted_accuracy"] = (weighted_accuracy, weighted_total)
    metric_values["acebench_unweighted_accuracy"] = (unweighted_accuracy, len(leaf_categories))
    if process_values:
        metric_values["acebench_process_accuracy"] = (
            sum(process_values) / len(process_values),
            len(process_values),
        )

    _write_jsonl(output_dir / "byob_predictions.jsonl", predictions)
    scores_payload = {
        metric_name: {
            "stats": {
                "count": count,
                "mean": round(value, 6),
                "stddev": 0.0,
                "stderr": 0.0,
            },
            "value": value,
        }
        for metric_name, (value, count) in metric_values.items()
    }
    _write_json(
        output_dir / "byob_results.json",
        {
            "tasks": {
                args.benchmark_name: {
                    "metrics": {
                        "pass@1": {
                            "scores": scores_payload,
                        }
                    }
                }
            }
        },
    )

    finished_at = datetime.now(UTC)
    inference_time = max(0.0, (finished_at - started_at).total_seconds())
    successful_count = sum(1 for row in predictions if row.get("error") in (None, ""))
    _write_json(
        output_dir / "eval_factory_metrics.json",
        {
            "response_stats": {
                "count": len(predictions),
                "successful_count": successful_count,
                "avg_latency_ms": 0.0,
                "avg_total_tokens": 0.0,
                "avg_completion_tokens": 0.0,
            },
            "timing": {
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "inference_time_seconds": inference_time,
            },
        },
    )
    _write_json(
        output_dir / "params.json",
        {
            "parallelism": parallelism,
            "request_timeout": max(1, int(args.request_timeout)),
            "limit_samples": None,
            "resume": resume,
            "show_live_stats": show_live_stats,
            "request_params": request_params,
            "resolved_categories": leaf_categories,
            "language": language,
            "model_fs_name": model_fs_name,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
