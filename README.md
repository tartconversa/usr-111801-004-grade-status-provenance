# 景区资料协作台账：降级摘牌状态的来源裁决

服务登记资料来源、字段、原件、使用许可、数据产品与交付凭据。示例主体和记录均为虚构，不连接真实文旅部门。

需要 Python 3.10 或更新版本，不依赖第三方包。

运行项目测试：python -m unittest discover -s tests -v。
检查源码：python -m compileall -q exchange。
运行本地演练：python -m exchange --database demo.db。

exchange/catalog.py 登记来源字段和原件，grants.py 与 policy.py 管理使用边界，products.py 编排资料，delivery.py 出具回执，audit.py 记录操作。ratings.py 负责景区等级的来源裁决：关联来源公告、决定效力与公开状态，区分待整改、已生效降级、主动退出、摘牌与待核，并让每次展示的等级都能回到所依据的原件（语义见 docs/domain.md）。目录职责及当前边界可从源码与测试读取。
