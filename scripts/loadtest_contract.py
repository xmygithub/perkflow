"""压测文件合同；只读本轮清单，不读取开发配置或连接服务。"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / ".local" / "loadtest"
RUN_PATTERN = r"[0-9a-f]{12}"
PREFIX_FIELDS = {
    "redis_key_prefix": "auth:",
    "cache_key_prefix": "cache:",
    "cache_lock_key_prefix": "cache-lock:",
    "order_id_key_prefix": "id:",
    "order_lock_key_prefix": "order-lock:",
    "seckill_key_prefix": "seckill:",
}


def directory(run_id):
    # 1. 参数只能是本脚本生成的短编号；不接受用户库名、任意目录或开发配置路径。
    if not re.fullmatch(RUN_PATTERN, run_id):
        raise ValueError("压测编号必须为12位小写十六进制")
    result = (RUNS / run_id).resolve()
    if result != ROOT.resolve() / ".local" / "loadtest" / run_id:
        raise ValueError("压测目录不能通过链接跳出工作区固定位置")
    return result


def identity(run_id):
    directory(run_id)
    namespace = f"perkflow:loadtest:{run_id}:"
    return {
        "mysql_database": f"perkflow_loadtest_{run_id}",
        "rabbitmq_prefix": f"perkflow.loadtest.{run_id}",
        **{name: namespace + suffix for name, suffix in PREFIX_FIELDS.items()},
    }


def load_manifest(run_id):
    value = json.loads((directory(run_id) / "manifest.json").read_text(encoding="utf-8"))
    if value.get("run_id") != run_id or value.get("status") != "ready":
        raise ValueError("压测环境未准备完成")
    if value.get("resources") != identity(run_id):
        raise ValueError("压测命名空间不符合本轮隔离合同")
    if value.get("mode") not in {"sync", "async"}:
        raise ValueError("未知压测模式")
    if value.get("scenario") not in {"distinct", "same-intent", "same-user"}:
        raise ValueError("未知购买场景")
    if not 8001 <= value.get("port", 0) <= 8999:
        raise ValueError("压测端口必须在8001至8999之间")
    for key in ("accounts", "attempts", "stock"):
        if type(value.get(key)) is not int or not 1 <= value[key] <= 100000:
            raise ValueError("压测规模超出本机准备上限")
    if value["accounts"] != (value["attempts"] if value["scenario"] == "distinct" else 1):
        raise ValueError("账号和购买尝试数不匹配")
    return value


def target(manifest):
    return f"http://127.0.0.1:{manifest['port']}"


def write_json(path, value):
    # 2. 结果先落临时文件再替换，便于中断后读取完整的上一份快照。
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_attempts(folder, manifest):
    """只恢复已存在的尝试事实；不补造成功、性能参数或缺失ticket。"""
    candidates, issues = {}, []
    snapshot = folder / "observations.json"
    if snapshot.exists():
        try:
            saved = json.loads(snapshot.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("run_id") != manifest["run_id"]:
                raise ValueError("快照不属于本轮")
            candidates["observations.json"] = saved["attempts"]
        except (ValueError, KeyError, TypeError):
            issues.append("INVALID_SNAPSHOT")
    journal = folder / "attempts.jsonl"
    if journal.exists():
        # 写入方每条flush。并发读取时只采纳完整行，半行保留诊断，不能算完成。
        candidates["attempts.jsonl"] = []
        lines = journal.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, line in enumerate(lines):
            if index == len(lines) - 1 and not line.endswith("\n"):
                issues.append("INCOMPLETE_JOURNAL_TAIL")
                break
            try:
                candidates["attempts.jsonl"].append(json.loads(line))
            except ValueError:
                issues.append("INVALID_JOURNAL_ROW")
                break
    valid = {}
    for source, rows in candidates.items():
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or type(row.get("ticket")) is not int
            or not 0 <= row["ticket"] < manifest["attempts"]
            or row.get("outcome")
            not in {"succeeded", "rejected", "pending", "unresolved", "failed", "error"}
            for row in rows
        ):
            issues.append("INVALID_ATTEMPT_ROWS:" + source)
            continue
        if len({row["ticket"] for row in rows}) != len(rows):
            issues.append("DUPLICATE_TICKETS:" + source)
            continue
        valid[source] = rows
    if len(valid) == 2:
        left, right = ({row["ticket"]: row for row in rows} for rows in valid.values())
        if any(left[key] != right[key] for key in left.keys() & right.keys()):
            issues.append("CONFLICTING_OBSERVATION_SOURCES")
    source = max(valid, key=lambda name: len(valid[name]), default="missing")
    if source == "missing":
        issues.append("NO_OBSERVATION_EVIDENCE")
    return valid.get(source, []), {"source": source, "valid": not issues, "issues": issues}


def classify_response(status, body, mode):
    # 3. 只将明确的409业务拒绝单列；鉴权错误、活动未开始、503等不能美化成正常售罄。
    code = body.get("error", {}).get("code") if isinstance(body, dict) else None
    expected = {"OUT_OF_STOCK", "PURCHASE_QUALIFICATION_BUSY", "ORDER_IN_PROGRESS"}
    if status == 409 and code in expected:
        return "rejected", code
    if status in {200, 201, 202} and isinstance(body, dict):
        if mode == "sync" and isinstance(body.get("order"), dict):
            if body["order"].get("status") == "created" and body["order"].get("id"):
                return "succeeded", None
        if mode == "async" and isinstance(body.get("request"), dict):
            request = body["request"]
            if request.get("id") and request.get("status") in {"pending", "succeeded", "failed"}:
                if request["status"] == "succeeded" and not (request.get("order") or {}).get("id"):
                    return "error", "INVALID_ORDER_RESULT"
                return request["status"], request.get("error_code")
    return "error", code or "HTTP_OR_PROTOCOL_ERROR"
