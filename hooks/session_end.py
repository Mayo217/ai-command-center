#!/usr/bin/env python3
"""SessionEnd hook: close the session row, pull cost/title from the transcript tail,
and auto-capture anything still unlogged so nothing is lost."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_common as hc
import db


def main(data):
    sid = data.get("session_id") or "unknown"
    con = db.connect()
    hc.ensure_session(con, data)
    reason = data.get("reason") or data.get("source") or data.get("matcher") or "other"
    meta = hc.transcript_meta(data.get("transcript_path"))
    con.execute(
        "UPDATE sessions SET ended_at=?, end_reason=?, title=COALESCE(?,title), model=COALESCE(?,model), "
        "cost_usd=COALESCE(?,cost_usd), lines_added=COALESCE(?,lines_added), lines_removed=COALESCE(?,lines_removed), "
        "model_usage=COALESCE(?,model_usage), updated_at=? WHERE session_id=?",
        (db.now_iso(), reason, meta.get("title"), meta.get("model"), meta.get("cost_usd"),
         meta.get("lines_added"), meta.get("lines_removed"), meta.get("model_usage"), db.now_iso(), sid))
    indexed = hc.index_transcript(data.get("transcript_path"), con)
    if meta.get("recap"):
        db.add_event(con, sid, "recap", {"text": meta["recap"][:1200]})
    item_id = hc.create_inferred_item(con, sid, data, "session_end")
    db.add_event(con, sid, "session_end", {"reason": reason, "auto_item": item_id, **indexed})
    db.log_line("hooks", f"session_end {sid[:8]} reason={reason} auto_item={item_id} index={indexed}")


if __name__ == "__main__":
    hc.run("session_end", main)
