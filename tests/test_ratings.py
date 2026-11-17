import tempfile
import unittest
from pathlib import Path

from exchange import Exchange


class RatingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Exchange(Path(self.temp.name) / "ratings.db")
        self.service.catalog.register(
            "src-a", "甲省文旅厅", "甲省", "rating",
            [("level", "景区等级", "text", False), ("finding", "复核发现", "text", False),
             ("reason", "事由", "text", False)],
            "2026-01-01",
        )
        self.service.catalog.register(
            "src-b", "乙市文旅局", "乙市", "rating",
            [("level", "景区等级", "text", False), ("finding", "复核发现", "text", False),
             ("reason", "事由", "text", False)],
            "2026-01-01",
        )
        self.service.catalog.register(
            "src-c", "甲省复核中心", "甲省", "rating",
            [("level", "景区等级", "text", False), ("finding", "复核发现", "text", False),
             ("reason", "事由", "text", False)],
            "2026-01-01",
        )

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def _spot(self, spot_id="s1", jurisdiction="甲省", source="src-a", level="4A"):
        return self.service.ratings.register_spot(
            spot_id, f"景区{spot_id}", jurisdiction, source,
            {"level": level, "reason": "授牌"}, "2026-01-05",
        )

    def _doc(self, document_id, spot_id, doc_type, source, payload, issued, effective=None,
             received=None, corrects=None):
        return self.service.ratings.import_document(
            document_id, spot_id, doc_type, source, payload, issued,
            effective or issued, received or issued, "受理员", corrects,
        )

    # 1. 整改通知 → 正式降级：业务状态可区分，等级能回到依据文件
    def test_rectification_then_effective_downgrade_keeps_traceable_basis(self):
        self._spot()
        self._doc("d-rect", "s1", "rectification", "src-a",
                  {"finding": "标识不清"}, "2026-03-01")
        view = self.service.ratings.current_view("s1")
        self.assertEqual(view["state"], "rectification")
        self.assertEqual(view["level"], "4A")
        self.assertEqual(view["basis"]["id"], "d-rect")
        self.assertEqual(view["basis"]["asset_id"], "d-rect")

        self._doc("d-down", "s1", "downgrade", "src-a",
                  {"level": "3A", "reason": "复核不达标"}, "2026-05-01")
        view = self.service.ratings.current_view("s1")
        self.assertEqual(view["state"], "downgraded")
        self.assertEqual(view["level"], "3A")
        self.assertEqual(view["basis"]["id"], "d-down")
        chain = self.service.ratings.decision_chain("s1")
        kinds = [(d["kind"], d["status"]) for d in chain]
        self.assertIn(("downgrade", "effective"), kinds)
        self.assertIn(("rectification", "closed"), kinds)
        # 每次展示的等级都能回到原件资产
        self.assertEqual(
            {a for a in self.service.ratings.dossier_asset_ids("s1")},
            {"s1-award-0", "d-rect", "d-down"},
        )

    # 2. 主动退出与摘牌是不同状态
    def test_voluntary_exit_and_delist_are_distinct_states(self):
        self._spot("s2")
        self._doc("d-exit", "s2", "voluntary_exit", "src-a",
                  {"reason": "企业申请"}, "2026-04-01")
        view = self.service.ratings.current_view("s2")
        self.assertEqual(view["state"], "exited")
        self.assertIsNone(view["level"])

        self._spot("s3")
        self._doc("d-delist", "s3", "delist", "src-a",
                  {"reason": "严重不达标"}, "2026-04-01")
        self.assertEqual(self.service.ratings.current_view("s3")["state"], "delisted")

    # 3. 重复导入幂等：同编号或同摘要都不会覆盖有效决定
    def test_duplicate_import_is_idempotent(self):
        self._spot()
        self._doc("d-down", "s1", "downgrade", "src-a",
                  {"level": "3A", "reason": "复核不达标"}, "2026-05-01")
        # 同编号重复导入
        self.assertEqual(
            self._doc("d-down", "s1", "downgrade", "src-a",
                      {"level": "3A", "reason": "复核不达标"}, "2026-05-01",
                      received="2026-06-01"),
            "d-down",
        )
        # 同摘要、新编号也视为同一份公告
        self.assertEqual(
            self._doc("d-down-copy", "s1", "downgrade", "src-a",
                      {"level": "3A", "reason": "复核不达标"}, "2026-05-01",
                      received="2026-06-02"),
            "d-down",
        )
        decisions = self.service.ratings.decision_chain("s1")
        self.assertEqual(len([d for d in decisions if d["kind"] == "downgrade"]), 1)
        self.assertEqual(self.service.ratings.current_view("s1")["basis"]["id"], "d-down")

    # 4. 迟到更正：后到的更正文件撤回旧决定，按更正关系生效，与导入先后无关
    def test_late_correction_withdraws_effective_decision(self):
        self._spot()
        self._doc("d-down", "s1", "downgrade", "src-a",
                  {"level": "3A", "reason": "复核不达标"}, "2026-05-01")
        # 更正通知 6 月才到，发文日 5 月 10 日
        self._doc("d-fix", "s1", "correction", "src-a",
                  {"level": "4A", "reason": "复核材料误录，恢复等级"},
                  "2026-05-10", received="2026-06-10", corrects="d-down")
        view = self.service.ratings.current_view("s1")
        self.assertEqual(view["state"], "rated")
        self.assertEqual(view["level"], "4A")
        self.assertEqual(view["basis"]["id"], "d-fix")
        chain = {d["document_id"]: d for d in self.service.ratings.decision_chain("s1")}
        self.assertEqual(chain["d-down"]["status"], "withdrawn")
        self.assertEqual(chain["d-fix"]["kind"], "award")
        self.assertEqual(chain["d-fix"]["supersedes_id"], chain["d-down"]["id"])

    # 4b. 撤回后没有其他有效等级依据时，保留待核，不推断等级
    def test_withdraw_without_replacement_leaves_unverified(self):
        self._spot()
        self._doc("d-w", "s1", "withdraw", "src-a",
                  {"reason": "授牌文件作废"}, "2026-02-01", corrects="s1-award-0")
        view = self.service.ratings.current_view("s1")
        self.assertEqual(view["state"], "unverified")
        self.assertIsNone(view["level"])
        self.assertEqual(view["reason"], "no_effective_basis")

    # 5. 辖区转移：转移后旧辖区公告不再管辖，不能覆盖有效决定
    def test_jurisdiction_transfer_blocks_old_owner_documents(self):
        self._spot()
        self.service.ratings.transfer_jurisdiction("s1", "乙市", "2026-02-01", "甲省文旅厅", "区划调整")
        self.assertEqual(self.service.ratings.jurisdiction_at("s1", "2026-01-15"), "甲省")
        self.assertEqual(self.service.ratings.jurisdiction_at("s1", "2026-03-01"), "乙市")
        # 旧辖区在转移后发布的降级公告
        self._doc("d-old", "s1", "downgrade", "src-a",
                  {"level": "3A", "reason": "旧辖区复核"}, "2026-03-01")
        view = self.service.ratings.current_view("s1")
        self.assertEqual(view["state"], "rated")
        self.assertEqual(view["level"], "4A")
        # 新辖区的公告正常生效
        self._doc("d-new", "s1", "downgrade", "src-b",
                  {"level": "3A", "reason": "新辖区复核"}, "2026-04-01")
        view = self.service.ratings.current_view("s1")
        self.assertEqual(view["state"], "downgraded")
        self.assertEqual(view["level"], "3A")
        self.assertEqual(view["basis"]["id"], "d-new")

    # 6. 证据冲突：同日不同等级 → 待核，不推断等级；候选依据全部留痕
    def test_same_day_conflicting_levels_keep_unverified(self):
        self._spot()
        self._doc("d-x", "s1", "downgrade", "src-a",
                  {"level": "3A", "reason": "复核不达标"}, "2026-05-01")
        self._doc("d-y", "s1", "downgrade", "src-c",
                  {"level": "2A", "reason": "同辖区另一来源同日结论"}, "2026-05-01")
        view = self.service.ratings.current_view("s1")
        self.assertEqual(view["state"], "unverified")
        self.assertIsNone(view["level"])
        self.assertEqual(view["reason"], "conflicting_evidence")
        candidates = {c["id"] for c in view["candidate_basis"]}
        self.assertIn("d-x", candidates)
        self.assertIn("d-y", candidates)
        # 人工复核关闭冲突并明确以 d-x 为准：落选决定撤回，系统不替业务猜等级
        conflict_id = view["conflicts"][0]["id"]
        self.service.ratings.resolve_conflict(conflict_id, "2026-05-05", "复核组",
                                               "以 d-x 为准", winning_document_id="d-x")
        view = self.service.ratings.current_view("s1")
        self.assertEqual(view["state"], "downgraded")
        self.assertEqual(view["level"], "3A")
        self.assertEqual(view["basis"]["id"], "d-x")
        chain = {d["document_id"]: d for d in self.service.ratings.decision_chain("s1")}
        self.assertEqual(chain["d-y"]["status"], "withdrawn")

    # 7. 迟到且发文日更早的相反等级决定，不能覆盖有效决定，进入待核
    def test_late_contradiction_does_not_override_effective_decision(self):
        self._spot()
        # 现行有效：5 月降级 3A
        self._doc("d-down", "s1", "downgrade", "src-a",
                  {"level": "3A", "reason": "复核不达标"}, "2026-05-01",
              received="2026-05-02")
        # 7 月才到一份发文日 4 月的 2A 决定（同辖区另一来源，迟到但有管辖权）
        self._doc("d-late", "s1", "downgrade", "src-c",
                  {"level": "2A", "reason": "迟到的更早决定"}, "2026-04-01",
              received="2026-07-01")
        view = self.service.ratings.current_view("s1")
        self.assertEqual(view["state"], "unverified")
        self.assertIsNone(view["level"])
        self.assertTrue(any(c["reason"] == "late_import_contradiction" for c in view["conflicts"]))
        # 撤回迟到文件后冲突解除，原有效决定恢复展示
        self._doc("d-late-w", "s1", "withdraw", "src-c",
                  {"reason": "文件误发撤回"}, "2026-07-05", corrects="d-late")
        view = self.service.ratings.current_view("s1")
        self.assertEqual(view["state"], "downgraded")
        self.assertEqual(view["level"], "3A")
        self.assertEqual(view["basis"]["id"], "d-down")

    # 8. 门户与地方名单公开时间不一致，分别留痕且可溯源
    def test_publication_channels_recorded_separately_and_idempotently(self):
        self._spot()
        self._doc("d-down", "s1", "downgrade", "src-a",
                  {"level": "3A", "reason": "复核不达标"}, "2026-05-01")
        self.service.ratings.publish("d-down", "local_list", "2026-05-03", "受理员")
        self.service.ratings.publish("d-down", "portal", "2026-05-10", "门户管理员")
        pubs = self.service.ratings.publications("d-down")
        self.assertEqual([(p["channel"], p["published_at"]) for p in pubs],
                         [("local_list", "2026-05-03"), ("portal", "2026-05-10")])
        # 重复公开幂等，不新增记录
        before = len(pubs)
        self.service.ratings.publish("d-down", "portal", "2026-05-10", "门户管理员")
        self.assertEqual(len(self.service.ratings.publications("d-down")), before)
        basis = self.service.ratings.current_view("s1")["basis"]
        self.assertEqual({p["channel"] for p in basis["publications"]}, {"portal", "local_list"})

    # 9. 决定链上的等级都能回到来源原件，并可编入既有数据产品交付
    def test_rating_basis_flows_into_product_and_delivery(self):
        self._spot()
        self._doc("d-down", "s1", "downgrade", "src-a",
                  {"level": "3A", "reason": "复核不达标"}, "2026-05-01")
        asset_ids = self.service.ratings.dossier_asset_ids("s1")
        self.service.grants.issue("g-1", "src-a", "复核组", "质量复核", "2026-12-31", "2026-05-02")
        self.service.policies.set("甲省", "rating", "质量复核", "allow", "2026-05-02", "甲省文旅厅")
        revision = self.service.products.assemble("dossier", "s1等级台账", asset_ids, "2026-05-03", "受理员")
        inputs = self.service.products.inputs("dossier", revision)
        self.assertEqual({i["id"] for i in inputs}, set(asset_ids))
        receipt = self.service.delivery.send("d-1", "dossier", revision, "复核组", "质量复核", "2026-05-04")
        self.assertEqual(receipt["product_revision"], revision)
        # 原件内容仍可从资产读取
        asset = self.service.catalog.get("d-down")
        self.assertIn("3A", asset["payload"])

    # 10. 多次辖区转移：历史时点辖区可回溯，当前辖区取最近一次转移
    def test_multiple_jurisdiction_moves_keep_full_history(self):
        self._spot()
        self.service.ratings.transfer_jurisdiction("s1", "乙市", "2026-02-01", "甲省文旅厅")
        self.service.catalog.register(
            "src-d", "丙区文旅局", "丙区", "rating",
            [("level", "景区等级", "text", False), ("reason", "事由", "text", False)],
            "2026-01-01",
        )
        self.service.ratings.transfer_jurisdiction("s1", "丙区", "2026-06-01", "乙市文旅局")
        self.assertEqual(self.service.ratings.jurisdiction_at("s1", "2026-01-15"), "甲省")
        self.assertEqual(self.service.ratings.jurisdiction_at("s1", "2026-03-01"), "乙市")
        self.assertEqual(self.service.ratings.jurisdiction_at("s1", "2026-07-01"), "丙区")
        self.assertEqual(self.service.ratings.current_jurisdiction("s1"), "丙区")
        with self.assertRaises(ValueError):
            self.service.ratings.transfer_jurisdiction("s1", "丙区", "2026-08-01", "丙区文旅局")

    # 11. 无等级的事实更正只维持整改观察，不产生新等级
    def test_correction_without_level_keeps_rectification(self):
        self._spot()
        self._doc("d-rect", "s1", "rectification", "src-a",
                  {"finding": "标识不清"}, "2026-03-01")
        self._doc("d-fix-note", "s1", "correction", "src-a",
                  {"finding": "标识不清，补充整改时限"}, "2026-03-10", corrects="d-rect")
        view = self.service.ratings.current_view("s1")
        self.assertEqual(view["state"], "rectification")
        self.assertEqual(view["level"], "4A")
        chain = {d["document_id"]: d for d in self.service.ratings.decision_chain("s1")}
        self.assertEqual(chain["d-rect"]["status"], "withdrawn")
        self.assertEqual(chain["d-fix-note"]["kind"], "rectification")

    # 12. 跨景区的同内容公告不应被当成重复导入
    def test_same_payload_across_spots_is_not_duplicate(self):
        self._spot("s1")
        self._spot("s9")
        first = self._doc("d-shared-1", "s1", "downgrade", "src-a",
                          {"level": "3A", "reason": "同一模板"}, "2026-05-01")
        second = self._doc("d-shared-2", "s9", "downgrade", "src-a",
                           {"level": "3A", "reason": "同一模板"}, "2026-05-01")
        self.assertEqual(first, "d-shared-1")
        self.assertEqual(second, "d-shared-2")
        self.assertEqual(self.service.ratings.current_view("s1")["level"], "3A")
        self.assertEqual(self.service.ratings.current_view("s9")["level"], "3A")


if __name__ == "__main__":
    unittest.main()
