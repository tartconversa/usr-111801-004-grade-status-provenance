import argparse
import json

from .service import Exchange


def main():
    parser = argparse.ArgumentParser(description="景区资料本地协作演练")
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    service = Exchange(args.database)
    try:
        # 来源目录与字段
        service.catalog.register(
            "src-a", "甲省文旅厅", "甲省", "rating",
            [("level", "景区等级", "text", False), ("finding", "复核发现", "text", False),
             ("reason", "事由", "text", False)],
            "2026-01-01",
        )
        service.catalog.register(
            "src-c", "甲省复核中心", "甲省", "rating",
            [("level", "景区等级", "text", False), ("reason", "事由", "text", False)],
            "2026-01-01",
        )

        # 景区登记（授牌原件建立初始有效等级 4A）
        service.ratings.register_spot(
            "lake", "云栖湖景区", "甲省", "src-a",
            {"level": "4A", "reason": "授牌"}, "2026-01-05",
        )

        # 整改通知：进入待整改，等级仍展示 4A，依据是整改公告
        service.ratings.import_document(
            "notice-rect", "lake", "rectification", "src-a",
            {"finding": "游客中心标识不清"}, "2026-03-01", "2026-03-01", "2026-03-02", "受理员",
        )
        # 正式降级决定生效：3A
        service.ratings.import_document(
            "notice-down", "lake", "downgrade", "src-a",
            {"level": "3A", "reason": "复核不达标"}, "2026-05-01", "2026-05-01", "2026-05-02", "受理员",
        )
        # 地方名单先公开，门户晚一周才更新
        service.ratings.publish("notice-down", "local_list", "2026-05-03", "名单管理员")
        service.ratings.publish("notice-down", "portal", "2026-05-10", "门户管理员")

        # 迟到的更正通知（6 月才到，发文日 5 月 10 日）：撤回降级并恢复 4A
        service.ratings.import_document(
            "notice-fix", "lake", "correction", "src-a",
            {"level": "4A", "reason": "复核材料误录，恢复等级"},
            "2026-05-10", "2026-05-10", "2026-06-10", "受理员",
            corrects_document_id="notice-down",
        )

        view = service.ratings.current_view("lake")
        print("当前等级视图：")
        print(json.dumps(view, ensure_ascii=False, sort_keys=True, indent=2))
        print("\n完整决定链（每次展示等级都可回到依据文件）：")
        for decision in service.ratings.decision_chain("lake"):
            print(json.dumps(
                {"sequence": decision["sequence"], "kind": decision["kind"],
                 "status": decision["status"], "level": decision["level"],
                 "document": decision["document"]["id"],
                 "asset": decision["document"]["asset_id"],
                 "publications": decision["document"]["publications"]},
                ensure_ascii=False, sort_keys=True,
            ))

        # 依据原件编入数据产品，沿用既有许可与交付链路
        asset_ids = service.ratings.dossier_asset_ids("lake")
        service.grants.issue("g-1", "src-a", "复核组", "质量复核", "2026-12-31", "2026-06-11")
        service.policies.set("甲省", "rating", "质量复核", "allow", "2026-06-11", "甲省文旅厅")
        revision = service.products.assemble("dossier", "云栖湖等级台账", asset_ids, "2026-06-12", "受理员")
        receipt = service.delivery.send("d-1", "dossier", revision, "复核组", "质量复核", "2026-06-13")
        print("\n交付回执：")
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    finally:
        service.close()


if __name__ == "__main__":
    main()
