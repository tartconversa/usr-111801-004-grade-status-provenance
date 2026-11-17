"""景区等级的来源裁决。

把来源公告（rating_documents）、决定效力（rating_decisions 的状态与更迭关系）
和公开状态（rating_publications）关联起来。当前展示等级一律由有效决定推导，
每个等级都能回到所依据的原件资产；证据冲突时保留“待核”，不推断等级。
"""

import hashlib
import json

from .audit import write
from .catalog import Catalog

DOC_TYPES = {"award", "rectification", "downgrade", "delist", "withdraw", "voluntary_exit", "correction"}
LEVEL_KINDS = {"award", "downgrade"}
PUBLISH_CHANNELS = {"portal", "local_list"}

# 状态到业务视图状态的映射
VIEW_STATE = {
    "award": "rated",
    "rectification": "rectification",
    "downgrade": "downgraded",
    "delist": "delisted",
    "voluntary_exit": "exited",
    "withdraw": "unverified",
}


class Ratings:
    def __init__(self, connection):
        self.connection = connection

    # ---------- 景区与辖区 ----------

    def register_spot(self, spot_id, name, jurisdiction, source_id, payload, at, actor=None):
        """登记景区，并以授牌原件建立初始等级决定。"""
        if self.connection.execute("SELECT 1 FROM spots WHERE id=?", (spot_id,)).fetchone():
            raise ValueError("景区已登记")
        asset_id = f"{spot_id}-award-0"
        if self.connection.execute("SELECT 1 FROM assets WHERE id=?", (asset_id,)).fetchone():
            raise ValueError("授牌原件编号冲突")
        catalog = Catalog(self.connection)
        source = self.connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if source is None or not source["active"]:
            raise ValueError("来源不可用")
        if source["jurisdiction"] != jurisdiction:
            raise ValueError("来源辖区与景区辖区不一致")
        if not isinstance(payload, dict) or not payload.get("level"):
            raise ValueError("授牌原件缺少等级字段")
        level = str(payload["level"])
        digest = catalog.ingest(asset_id, source_id, payload, at)
        with self.connection:
            self.connection.execute(
                "INSERT INTO spots(id,name,jurisdiction,created_at) VALUES(?,?,?,?)",
                (spot_id, name, jurisdiction, at),
            )
            self.connection.execute(
                "INSERT INTO rating_documents(id,spot_id,doc_type,issuer,jurisdiction,issued_at,"
                "effective_at,target_level,source_id,asset_id,digest,received_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset_id, spot_id, "award", source["owner"], jurisdiction, at, at,
                 level, source_id, asset_id, digest, at),
            )
            self.connection.execute(
                "INSERT INTO rating_decisions(spot_id,document_id,kind,status,level,issued_at,"
                "effective_at,supersedes_id,sequence) VALUES(?,?,?,?,?,?,?,?,?)",
                (spot_id, asset_id, "award", "effective", level, at, at, None, 1),
            )
            write(self.connection, at, actor or source["owner"], "spot.register", spot_id,
                  {"jurisdiction": jurisdiction, "level": level, "document": asset_id})
        return asset_id

    def transfer_jurisdiction(self, spot_id, to_jurisdiction, at, actor, note=""):
        """辖区转移：此后旧辖区的公告不再管辖该景区，历史决定保留。"""
        spot = self.connection.execute("SELECT * FROM spots WHERE id=?", (spot_id,)).fetchone()
        if spot is None:
            raise KeyError(spot_id)
        current = self.current_jurisdiction(spot_id)
        if current == to_jurisdiction:
            raise ValueError("辖区未发生变化")
        if self.connection.execute(
            "SELECT 1 FROM spot_jurisdiction_moves WHERE spot_id=? AND effective_at=?",
            (spot_id, at),
        ).fetchone():
            raise ValueError("该生效日已存在辖区转移记录")
        with self.connection:
            seq_row = self.connection.execute(
                "SELECT COUNT(*) FROM spot_jurisdiction_moves WHERE spot_id=?", (spot_id,)
            ).fetchone()
            self.connection.execute(
                "INSERT INTO spot_jurisdiction_moves(spot_id,from_jurisdiction,to_jurisdiction,effective_at,note) "
                "VALUES(?,?,?,?,?)",
                (spot_id, current, to_jurisdiction, at, note),
            )
            # spots.jurisdiction 保留登记辖区；当前辖区由最近一次转移推导，历史时点才能回溯
            self.connection.execute(
                "UPDATE rating_documents SET governing=0 WHERE spot_id=? AND jurisdiction<>? AND issued_at>=?",
                (spot_id, to_jurisdiction, at),
            )
            write(self.connection, at, actor, "spot.jurisdiction_transfer", spot_id,
                  {"from": current, "to": to_jurisdiction, "effective_at": at, "move": seq_row[0] + 1})
        self._reconcile_conflicts(spot_id, at, actor)

    def jurisdiction_at(self, spot_id, at):
        """景区在某一时点所属的辖区（按转移生效日回溯）。"""
        spot = self.connection.execute("SELECT jurisdiction FROM spots WHERE id=?", (spot_id,)).fetchone()
        if spot is None:
            raise KeyError(spot_id)
        move = self.connection.execute(
            "SELECT from_jurisdiction,to_jurisdiction,effective_at FROM spot_jurisdiction_moves "
            "WHERE spot_id=? AND effective_at<=? ORDER BY effective_at DESC, id DESC LIMIT 1",
            (spot_id, at),
        ).fetchone()
        return move["to_jurisdiction"] if move else spot["jurisdiction"]

    def current_jurisdiction(self, spot_id):
        """景区当前辖区：取最近一次转移的目标辖区，无转移则为登记辖区。"""
        spot = self.connection.execute("SELECT jurisdiction FROM spots WHERE id=?", (spot_id,)).fetchone()
        if spot is None:
            raise KeyError(spot_id)
        move = self.connection.execute(
            "SELECT to_jurisdiction FROM spot_jurisdiction_moves WHERE spot_id=? "
            "ORDER BY effective_at DESC, id DESC LIMIT 1",
            (spot_id,),
        ).fetchone()
        return move["to_jurisdiction"] if move else spot["jurisdiction"]

    # ---------- 公告导入 ----------

    def import_document(self, document_id, spot_id, doc_type, source_id, payload, issued_at,
                        effective_at, at, actor=None, corrects_document_id=None):
        """导入一份来源公告并生成/更新决定。

        幂等：同编号或同摘要重复导入直接返回既有编号，不产生任何覆盖。
        迟到更正与撤回按文件本身的发文日期与更正关系处理，与导入先后无关。
        """
        if doc_type not in DOC_TYPES:
            raise ValueError("公告类型无效")
        spot = self.connection.execute("SELECT * FROM spots WHERE id=?", (spot_id,)).fetchone()
        if spot is None:
            raise KeyError(spot_id)
        source = self.connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if source is None or not source["active"]:
            raise ValueError("来源不可用")

        preview_digest = self._preview_digest(source_id, payload)
        duplicate = self.connection.execute(
            "SELECT id FROM rating_documents WHERE spot_id=? AND (id=? OR digest=?)",
            (spot_id, document_id, preview_digest),
        ).fetchone()
        if duplicate:
            return duplicate["id"]

        if doc_type in LEVEL_KINDS:
            if not isinstance(payload, dict) or not payload.get("level"):
                raise ValueError("等级决定原件缺少目标等级")
            target_level = str(payload["level"])
        elif doc_type == "correction":
            # 更正可携带恢复后的等级，也可仅纠正事实；有则提取，无则维持整改观察
            target_level = str(payload["level"]) if isinstance(payload, dict) and payload.get("level") else None
        else:
            target_level = None
        if corrects_document_id is not None:
            corrected = self.connection.execute(
                "SELECT * FROM rating_documents WHERE id=? AND spot_id=?",
                (corrects_document_id, spot_id),
            ).fetchone()
            if corrected is None:
                raise ValueError("更正对象不存在")
        if doc_type == "withdraw" and corrects_document_id is None:
            raise ValueError("撤回公告必须指明被撤回文件")

        asset_id = document_id
        if self.connection.execute("SELECT 1 FROM assets WHERE id=?", (asset_id,)).fetchone():
            raise ValueError("原件编号与既有资产冲突")
        digest = Catalog(self.connection).ingest(asset_id, source_id, payload, at)
        received_seq = self.connection.execute(
            "SELECT COALESCE(MAX(received_seq),0)+1 FROM rating_documents WHERE spot_id=?", (spot_id,)
        ).fetchone()[0]

        governing = 1 if self.jurisdiction_at(spot_id, issued_at) == source["jurisdiction"] else 0
        with self.connection:
            self.connection.execute(
                "INSERT INTO rating_documents(id,spot_id,doc_type,issuer,jurisdiction,issued_at,effective_at,"
                "target_level,source_id,asset_id,digest,corrects_document_id,governing,received_at,received_seq) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (document_id, spot_id, doc_type, source["owner"], source["jurisdiction"], issued_at,
                 effective_at, target_level, source_id, asset_id, digest, corrects_document_id,
                 governing, at, received_seq),
            )
            write(self.connection, at, actor or source["owner"], "rating.document_import", document_id,
                  {"spot": spot_id, "doc_type": doc_type, "governing": governing,
                   "issued_at": issued_at, "received_at": at})
            if not governing:
                write(self.connection, at, actor or source["owner"], "rating.document_nongoverning", document_id,
                      {"spot": spot_id, "jurisdiction": source["jurisdiction"],
                       "spot_jurisdiction": self.jurisdiction_at(spot_id, issued_at)})
            else:
                self._apply_governing_document(document_id, spot_id, doc_type, target_level,
                                               issued_at, effective_at, corrects_document_id, at,
                                               actor or source["owner"])
        self._reconcile_conflicts(spot_id, at, actor or source["owner"])
        return document_id

    def _apply_governing_document(self, document_id, spot_id, doc_type, target_level,
                                  issued_at, effective_at, corrects_document_id, at, actor):
        if doc_type == "correction":
            # 更正文件本身会产生替代决定，这里只撤回原决定，不再插入撤回标记
            self._mark_withdrawn(corrects_document_id, spot_id, at, actor, kind="correction")
            # 带目标等级的更正是恢复/重定等级的授牌类决定；无等级的更正仅维持整改观察
            kind = "award" if target_level is not None else "rectification"
            self._insert_decision(document_id, spot_id, kind, target_level, issued_at,
                                  effective_at, corrects_document_id, at, actor)
            return
        if doc_type == "withdraw":
            self._withdraw_decision(corrects_document_id, document_id, spot_id, at, actor, kind="withdraw")
            return
        # 产生等级/状态决定的公告
        self._insert_decision(document_id, spot_id, doc_type, target_level, issued_at,
                              effective_at, None, at, actor)

    def _insert_decision(self, document_id, spot_id, kind, level, issued_at, effective_at,
                         supersedes_document_id, at, actor):
        latest = self.connection.execute(
            "SELECT id,kind,status,level FROM rating_decisions WHERE spot_id=? "
            "ORDER BY issued_at DESC, sequence DESC LIMIT 1",
            (spot_id,),
        ).fetchone()
        # 整改观察不改变等级：继承当前有效等级，使整改期间展示的等级仍有依据
        if kind == "rectification" and level is None:
            level = latest["level"] if latest else None
        supersedes_id = None
        if supersedes_document_id is not None:
            target = self.connection.execute(
                "SELECT id FROM rating_decisions WHERE document_id=?", (supersedes_document_id,)
            ).fetchone()
            supersedes_id = target["id"] if target else None
        elif latest is not None and kind in LEVEL_KINDS and latest["kind"] == "rectification" \
                and latest["status"] == "open":
            supersedes_id = latest["id"]
        sequence = self.connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM rating_decisions WHERE spot_id=?", (spot_id,)
        ).fetchone()[0]
        status = "open" if kind == "rectification" else "effective"
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO rating_decisions(spot_id,document_id,kind,status,level,issued_at,"
                "effective_at,supersedes_id,sequence) VALUES(?,?,?,?,?,?,?,?,?)",
                (spot_id, document_id, kind, status, level, issued_at,
                 effective_at, supersedes_id, sequence),
            )
            if supersedes_id:
                self.connection.execute(
                    "UPDATE rating_decisions SET status='closed' WHERE id=? AND status='open'",
                    (supersedes_id,),
                )
            write(self.connection, at, actor, "rating.decision", document_id,
                  {"spot": spot_id, "decision": cursor.lastrowid, "kind": kind, "status": status,
                   "level": level, "supersedes": supersedes_id, "sequence": sequence})

    def _mark_withdrawn(self, target_document_id, spot_id, at, actor, kind):
        target = self.connection.execute(
            "SELECT * FROM rating_decisions WHERE document_id=? AND spot_id=?",
            (target_document_id, spot_id),
        ).fetchone()
        if target is None:
            raise ValueError("被撤回文件没有对应决定")
        self.connection.execute(
            "UPDATE rating_decisions SET status='withdrawn' WHERE id=?", (target["id"],)
        )
        write(self.connection, at, actor, "rating.decision_withdrawn", target_document_id,
              {"spot": spot_id, "kind": kind})
        return target

    def _withdraw_decision(self, target_document_id, withdraw_document_id, spot_id, at, actor, kind):
        target = self._mark_withdrawn(target_document_id, spot_id, at, actor, kind)
        sequence = self.connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM rating_decisions WHERE spot_id=?", (spot_id,)
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO rating_decisions(spot_id,document_id,kind,status,level,issued_at,"
            "effective_at,supersedes_id,sequence) VALUES(?,?,?,?,?,?,?,?,?)",
            (spot_id, withdraw_document_id, kind, "effective", None,
             at, at, target["id"], sequence),
        )

    # ---------- 公开状态 ----------

    def publish(self, document_id, channel, published_at, actor):
        """登记某份公告在门户或地方名单上的公开；同渠道重复登记幂等。"""
        if channel not in PUBLISH_CHANNELS:
            raise ValueError("公开渠道无效")
        document = self.connection.execute(
            "SELECT spot_id FROM rating_documents WHERE id=?", (document_id,)
        ).fetchone()
        if document is None:
            raise KeyError(document_id)
        with self.connection:
            existing = self.connection.execute(
                "SELECT id,published_at FROM rating_publications WHERE document_id=? AND channel=?",
                (document_id, channel),
            ).fetchone()
            if existing:
                return existing["id"]
            cursor = self.connection.execute(
                "INSERT INTO rating_publications(document_id,channel,published_at) VALUES(?,?,?)",
                (document_id, channel, published_at),
            )
            write(self.connection, published_at, actor, "rating.publish", document_id,
                  {"channel": channel, "spot": document["spot_id"]})
            return cursor.lastrowid

    def publications(self, document_id):
        rows = self.connection.execute(
            "SELECT channel,published_at FROM rating_publications WHERE document_id=? ORDER BY published_at,id",
            (document_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 当前等级视图与溯源 ----------

    def current_view(self, spot_id):
        """业务人员使用的当前等级视图：状态、展示等级、依据文件与公开情况。"""
        spot = self.connection.execute("SELECT * FROM spots WHERE id=?", (spot_id,)).fetchone()
        if spot is None:
            raise KeyError(spot_id)
        current_jurisdiction = self.current_jurisdiction(spot_id)
        active_conflicts = self.connection.execute(
            "SELECT * FROM rating_conflicts WHERE spot_id=? AND active=1 ORDER BY id", (spot_id,)
        ).fetchall()
        chain = self._decision_chain(spot_id)
        if active_conflicts:
            candidates = [self._document_brief(row["document_id"]) for row in chain
                          if row["status"] != "withdrawn" and row["kind"] in LEVEL_KINDS]
            return {
                "spot_id": spot_id,
                "name": spot["name"],
                "jurisdiction": current_jurisdiction,
                "state": "unverified",
                "level": None,
                "reason": "conflicting_evidence",
                "basis": None,
                "conflicts": [dict(row) for row in active_conflicts],
                "candidate_basis": candidates,
            }
        governing_docs = self.connection.execute(
            "SELECT id FROM rating_documents WHERE spot_id=? AND governing=1", (spot_id,)
        ).fetchall()
        if not governing_docs:
            return {
                "spot_id": spot_id, "name": spot["name"], "jurisdiction": current_jurisdiction,
                "state": "unverified", "level": None, "reason": "no_effective_basis",
                "basis": None, "conflicts": [], "candidate_basis": [],
            }
        latest = next((row for row in chain
                       if row["status"] != "withdrawn" and row["kind"] not in {"withdraw", "correction"}),
                      None)
        if latest is None:
            return {
                "spot_id": spot_id, "name": spot["name"], "jurisdiction": current_jurisdiction,
                "state": "unverified", "level": None, "reason": "no_effective_basis",
                "basis": None, "conflicts": [], "candidate_basis": [],
            }
        brief = self._document_brief(latest["document_id"])
        return {
            "spot_id": spot_id,
            "name": spot["name"],
            "jurisdiction": current_jurisdiction,
            "state": VIEW_STATE[latest["kind"]],
            "level": latest["level"],
            "reason": None,
            "basis": brief,
            "conflicts": [],
            "candidate_basis": [],
        }

    def decision_chain(self, spot_id):
        """完整决定链（含已撤回/已关闭），用于从每次展示等级回到依据文件。"""
        return [self._with_document(row) for row in self._decision_chain(spot_id)]

    def dossier_asset_ids(self, spot_id):
        """该景区全部有效管辖公告对应的原件资产，可直接编入数据产品。"""
        rows = self.connection.execute(
            "SELECT asset_id FROM rating_documents WHERE spot_id=? AND governing=1 ORDER BY issued_at,id",
            (spot_id,),
        ).fetchall()
        return [row["asset_id"] for row in rows]

    # ---------- 冲突裁决 ----------

    def _reconcile_conflicts(self, spot_id, at, actor):
        """重新计算冲突并同步冲突台账。

        两类情形保留待核，不推断等级：
        1. 同发文日的两份等级决定目标等级不一致（同日等级冲突）；
        2. 迟到导入的等级决定，发文日早于现行有效决定且等级相反（迟到矛盾）。
        整改观察不参与等级冲突；正常更迭（发文日更晚的决定取代在先决定）不算冲突。
        撤回/更正使矛盾消失时，既有冲突自动解除。
        """
        level_docs = self.connection.execute(
            "SELECT d.id,d.issued_at,d.target_level,d.received_seq "
            "FROM rating_documents d JOIN rating_decisions dec ON dec.document_id=d.id "
            "WHERE d.spot_id=? AND d.doc_type IN ('award','downgrade') AND d.governing=1 "
            "AND dec.status<>'withdrawn' ORDER BY d.issued_at,d.id",
            (spot_id,),
        ).fetchall()
        found = []
        for i, doc in enumerate(level_docs):
            for other in level_docs[i + 1:]:
                if doc["target_level"] == other["target_level"]:
                    continue
                if other["issued_at"] == doc["issued_at"]:
                    found.append(("same_day_level_conflict", other["id"], doc["id"]))
                elif other["issued_at"] > doc["issued_at"]:
                    # other 是发文日更晚的现行决定；doc 若到得更晚即为迟到矛盾
                    if doc["received_seq"] > other["received_seq"]:
                        found.append(("late_import_contradiction", doc["id"], other["id"]))
        with self.connection:
            existing = {
                (row["reason"], row["document_id"], row["other_document_id"])
                for row in self.connection.execute(
                    "SELECT * FROM rating_conflicts WHERE spot_id=? AND active=1", (spot_id,)
                ).fetchall()
            }
            for reason, later_id, earlier_id in found:
                key = (reason, later_id, earlier_id)
                if key in existing:
                    existing.discard(key)
                    continue
                self.connection.execute(
                    "INSERT INTO rating_conflicts(spot_id,reason,document_id,other_document_id,opened_at) "
                    "VALUES(?,?,?,?,?)",
                    (spot_id, reason, later_id, earlier_id, at),
                )
                write(self.connection, at, actor, "rating.conflict_opened", spot_id,
                      {"reason": reason, "document": later_id, "other": earlier_id})
            for reason, later_id, earlier_id in existing:
                self.connection.execute(
                    "UPDATE rating_conflicts SET active=0, resolved_at=? "
                    "WHERE spot_id=? AND reason=? AND document_id=? AND other_document_id=? AND active=1",
                    (at, spot_id, reason, later_id, earlier_id),
                )
                write(self.connection, at, actor, "rating.conflict_resolved", spot_id,
                      {"reason": reason, "document": later_id, "other": earlier_id})

    def resolve_conflict(self, conflict_id, at, actor, note="", winning_document_id=None):
        """人工复核后关闭冲突。

        可显式指定胜出文件：落选决定标记撤回，视图随后只按胜出依据推导；
        不指定时不改动任何决定，视图仍按决定链呈现并保留全部候选痕迹。
        系统本身不推断哪一等级正确。
        """
        conflict = self.connection.execute(
            "SELECT * FROM rating_conflicts WHERE id=? AND active=1", (conflict_id,)
        ).fetchone()
        if conflict is None:
            raise KeyError(conflict_id)
        with self.connection:
            if winning_document_id is not None:
                if winning_document_id not in (conflict["document_id"], conflict["other_document_id"]):
                    raise ValueError("胜出文件不属于该冲突")
                loser_id = conflict["other_document_id"] if winning_document_id == conflict["document_id"] \
                    else conflict["document_id"]
                loser = self.connection.execute(
                    "SELECT id FROM rating_decisions WHERE document_id=?", (loser_id,)
                ).fetchone()
                if loser is not None:
                    self.connection.execute(
                        "UPDATE rating_decisions SET status='withdrawn' WHERE id=?", (loser["id"],)
                    )
                write(self.connection, at, actor, "rating.conflict_loser_withdrawn", conflict["spot_id"],
                      {"conflict": conflict_id, "winner": winning_document_id, "loser": loser_id})
            self.connection.execute(
                "UPDATE rating_conflicts SET active=0, resolved_at=? WHERE id=?", (at, conflict_id)
            )
            write(self.connection, at, actor, "rating.conflict_manual_resolve", conflict["spot_id"],
                  {"conflict": conflict_id, "winner": winning_document_id, "note": note})

    # ---------- 辅助 ----------

    def _decision_chain(self, spot_id):
        rows = self.connection.execute(
            "SELECT * FROM rating_decisions WHERE spot_id=? ORDER BY issued_at DESC, sequence DESC",
            (spot_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _with_document(self, decision):
        result = dict(decision)
        result["document"] = self._document_brief(decision["document_id"])
        return result

    def _document_brief(self, document_id):
        doc = self.connection.execute(
            "SELECT d.*, s.owner AS source_owner FROM rating_documents d "
            "JOIN sources s ON s.id=d.source_id WHERE d.id=?",
            (document_id,),
        ).fetchone()
        if doc is None:
            return None
        result = dict(doc)
        result["publications"] = self.publications(document_id)
        return result

    def _preview_digest(self, source_id, payload):
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
