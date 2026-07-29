"""主营构成、官方公告事件、公开交易信息与大宗交易的强类型 `revision` 模型。

每种来源事实保留自己的业务键、单位、日期、身份解析和知识版本；公告标题、证券名称、金额或
同一交易日的相似记录都不足以自动合并，更不能把后续收益、当前市值等未来信息写回历史事实。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    desc,
)
from sqlalchemy.dialects.postgresql import DATERANGE, TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .revision_mixin import CanonicalRevisionMixin


class BusinessCompositionReportRevision(CanonicalRevisionMixin, Base):
    """保存证券报告期主营构成版本；合并范围、章节和币种不能静默合并。

    一份报告的主营构成可能按行业、产品或地区披露，且合并/母公司范围、报告章节和币种决定其可比性。
    内容更正或来源新观察追加 `revision`，不会覆盖旧披露；下级行只能在同一报告版本和维度内比较，
    不能与另一章节、另一币种或另一报告期自动求和。
    """

    __tablename__ = "business_composition_report_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_business_composition_report_revision_no"),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_business_composition_report_knowledge_range",
        ),
        UniqueConstraint(
            "report_period",
            "security_id",
            "report_type",
            "statement_scope",
            "disclosure_section",
            "methodology_version_id",
            "revision_no",
            name="uq_business_composition_report_revision",
        ),
        Index(
            "ix_business_composition_report_asof",
            "security_id",
            desc("report_period"),
            desc("known_from"),
        ),
        {
            "postgresql_partition_by": "RANGE (report_period)",
            "comment": "主营构成报告 revision 父表；报告期、合并范围和章节构成不可混用的逻辑键。",
        },
    )

    report_period: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="会计报告期末日期。"
    )
    report_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="主营构成报告 revision UUID。",
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="发行人 A 股永久证券内部键。",
    )
    report_type: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="年报、半年报或其他明确报告类型。"
    )
    statement_scope: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="合并、母公司或来源确认的报表范围。"
    )
    disclosure_section: Mapped[str] = mapped_column(
        String(48), nullable=False, comment="主营构成披露章节或来源栏目。"
    )
    currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="金额币种 ISO 代码；来源未披露时为空。"
    )
    currency_null_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="币种为空的受控原因，不能默认人民币。"
    )
    amount_unit: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="来源金额单位或缩放口径。"
    )
    announcement_document_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("disclosure_document.document_id", ondelete="RESTRICT"),
        nullable=True,
        comment="可选关联官方公告文档 UUID。",
    )
    public_usable_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="历史 PIT 最早安全使用时刻。"
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(report_period, NULL, '[)')", persisted=True),
        nullable=True,
        comment="报告期起点生成的开放有效范围，仅供时序审计。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="知识时间半开范围。",
    )


class BusinessCompositionLine(Base):
    """保存报告 `revision` 内的行业、产品或地区行，跨维度不允许自动求和。

    每行保留来源标签、可选受控映射、金额/比例/单位及其缺失原因；相同行名在不同维度可能含义不同。
    父子层级和汇总关系由来源结构表达，服务不为凑齐总额而修补或转化；任何跨行加总必须在明确的
    方法学、币种和范围条件下由派生层完成。
    """

    __tablename__ = "business_composition_line"
    __table_args__ = (
        CheckConstraint(
            "dimension IN ('INDUSTRY', 'PRODUCT', 'REGION')",
            name="ck_business_composition_line_dimension",
        ),
        ForeignKeyConstraint(
            ["report_period", "report_revision_id"],
            [
                "business_composition_report_revision.report_period",
                "business_composition_report_revision.report_revision_id",
            ],
            name="fk_business_composition_line_report",
            ondelete="RESTRICT",
        ),
        {
            "postgresql_partition_by": "RANGE (report_period)",
            "comment": "主营构成报告行父表；行业、产品和地区维度保持独立。",
        },
    )

    report_period: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="所属报告期末日期。"
    )
    report_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="所属主营构成报告 revision UUID。",
    )
    line_no: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="同一维度内来源行序号。"
    )
    dimension: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="行业、产品或地区维度。"
    )
    label_raw: Mapped[str] = mapped_column(
        String(300), nullable=False, comment="公司披露的原始标签文本。"
    )
    revenue: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露营业收入；缺失时为空。"
    )
    cost: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露营业成本；缺失时为空。"
    )
    profit: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露利润；缺失时为空。"
    )
    gross_margin: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="来源披露或可证实计算的毛利率；缺失时为空。"
    )
    null_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="数值字段为空的来源或口径原因。"
    )


class BusinessCompositionLabelVersion(Base):
    """保存公司自定义主营标签原文与可选精确映射，不做模糊 `taxonomy` 归并。

    来源标签随报告和公司表述变化，保留版本化原文才能解释历史分项；只有治理确认的精确映射才能
    连接受控分类体系。名称相近、翻译相似或当前公司业务相近都不足以自动归并，未知标签必须可见
    地保持未映射，避免把不同产品或行业的历史金额合在一起。
    """

    __tablename__ = "business_composition_label_version"
    __table_args__ = (
        CheckConstraint(
            "dimension IN ('INDUSTRY', 'PRODUCT', 'REGION')",
            name="ck_business_composition_label_dimension",
        ),
        CheckConstraint(
            "mapping_method IN ('EXACT', 'NONE')", name="ck_business_composition_label_mapping"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_business_composition_label_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_business_composition_label_knowledge_range",
        ),
        ExcludeConstraint(
            ("security_id", "="),
            ("dimension", "="),
            ("label_raw", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
            name="ex_business_composition_label_time",
        ),
        {"comment": "主营构成标签版本；无法精确映射的标签必须保留 NONE 状态。"},
    )

    label_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="主营标签版本 UUID。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="发行人永久证券内部键。",
    )
    dimension: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="行业、产品或地区维度。"
    )
    label_raw: Mapped[str] = mapped_column(
        String(300), nullable=False, comment="公司原始主营标签。"
    )
    normalized_label: Mapped[str | None] = mapped_column(
        String(300), nullable=True, comment="只有精确证据支持时的标准化标签。"
    )
    mapping_method: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="精确映射或未映射标记。"
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, comment="标签开始适用日期。")
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="标签停止适用日期；开区间为空。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始采用标签版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用标签版本的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑标签版本的来源批次。",
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="有效日期半开范围。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="知识时间半开范围。",
    )


class DisclosureDocument(Base):
    """保存官方公开文档永久身份；`URL` 是定位线索而不是业务主键。

    同一文档可能镜像、换域、补充附件或更正下载地址，因此永久键、发布机构和来源文档标识优先于
    链接文本。文档记录是事件、业绩、股本和持股事实的证据锚，不表示文档内容已经被完整解析或可
    公开再分发；受限正文仍只保留私有证据引用和摘要。
    """

    __tablename__ = "disclosure_document"
    __table_args__ = (
        CheckConstraint(
            "published_precision IN ('EXACT', 'DATE_ONLY', 'UNKNOWN')",
            name="ck_disclosure_document_published_precision",
        ),
        UniqueConstraint(
            "source_id", "source_document_id", name="uq_disclosure_document_source_key"
        ),
        Index(
            "ix_disclosure_document_security_published", "issuer_security_id", desc("published_at")
        ),
        {"comment": "官方披露文档永久身份；撤回和更正通过追加关系表达而不删除历史。"},
    )

    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="披露文档永久 UUID。"
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_source.source_id", ondelete="RESTRICT"),
        nullable=False,
        comment="文档真实上游来源 UUID。",
    )
    source_batch_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "source_batch.source_batch_id",
            name="fk_disclosure_document_source_batch",
            ondelete="RESTRICT",
        ),
        nullable=True,
        comment="最近确认该文档内容的来源观察批次；迁移前历史空值不得补造。",
    )
    source_document_id: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="上游稳定文档标识。"
    )
    issuer_security_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=True,
        comment="可准确解析的发行人证券；歧义时为空。",
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, comment="来源披露标题原文。")
    document_type: Mapped[str] = mapped_column(
        String(48), nullable=False, comment="公告、年报、快报或其他受控文档类型。"
    )
    announced_on: Mapped[date] = mapped_column(
        Date, nullable=False, comment="来源公告日期；日期精度不能伪装为精确发布时间。"
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="可验证官方发布时间；未知或仅日期时为空。"
    )
    published_precision: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="官方发布时间精度。"
    )
    official_url: Mapped[str] = mapped_column(
        Text, nullable=False, comment="官方文档链接；不作为永久身份。"
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="允许保存时的文档内容摘要；不存全文时为空。"
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="来源明确撤回时间；未撤回时为空。"
    )


class DisclosureDocumentRelation(Base):
    """记录文档的更正、补充、结果或撤回关系；关系图不能形成自环。

    关系让读取层理解后一份公告是补充、否定还是执行前一份，而不是按发布时间覆盖旧文档。两端都
    保留永久文档身份和关系类型，避免仅凭标题、证券或日期猜测关联；关系本身不是对业务事件是否
    完成的结论，仍需由相应事实 `revision` 和来源状态说明。
    """

    __tablename__ = "disclosure_document_relation"
    __table_args__ = (
        CheckConstraint(
            "from_document_id <> to_document_id", name="ck_disclosure_document_relation_not_self"
        ),
        {"comment": "披露文档关系；递归审计负责验证多节点关系没有环。"},
    )

    from_document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("disclosure_document.document_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="关系起点文档 UUID。",
    )
    to_document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("disclosure_document.document_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="关系终点文档 UUID。",
    )
    relation_type: Mapped[str] = mapped_column(
        String(24), primary_key=True, nullable=False, comment="更正、补充、结果或撤回关系类型。"
    )
    evidence_source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑文档关系的来源批次。",
    )


class CorporateEvent(Base):
    """保存跨文档公司业务事件身份，禁止按标题或人员姓名模糊合并。

    一个事件可有计划、进展、结果、更正和撤回等多份文件，事件锚让这些证据可聚合而不丢失文档
    原文。事件创建需要受控来源键或明确关系；同名主体、相似公告标题和同日披露都不能自动合并，
    以免将不同融资、并购或治理事项错误串联。
    """

    __tablename__ = "corporate_event"
    __table_args__ = (
        UniqueConstraint("source_id", "source_event_key", name="uq_corporate_event_source_key"),
        {"comment": "公司事件永久身份；不同文档对同一事件的知识版本在 revision 表追加。"},
    )

    event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="公司事件永久 UUID。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="事件关联发行人永久证券内部键。",
    )
    event_family: Mapped[str] = mapped_column(
        String(48), nullable=False, comment="业绩、解禁、股本或其他事件家族。"
    )
    source_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_source.source_id", ondelete="RESTRICT"),
        nullable=True,
        comment="来源事件键所属上游来源；无来源键时为空。",
    )
    source_event_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True, comment="来源稳定事件键；无稳定键时为空。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="事件身份首次建立时间。"
    )


class CorporateEventRevision(CanonicalRevisionMixin, Base):
    """保存公司事件知识版本；计划、进展、结果、更正和撤回不能原地覆盖。

    事件阶段、公告时间、业务日期和状态必须随每次可验证观察冻结，后续文件可改变平台理解但不改写
    历史。`revision` 与知识时间让用户能区分“当时只是计划”与“后来已经撤回”；它不应根据股价、
    新闻转载或人员同名推断事实，也不能作为未来收益或因果关系的输入。
    """

    __tablename__ = "corporate_event_revision"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('PLAN', 'PROGRESS', 'RESULT', 'CORRECTION', 'WITHDRAWAL')",
            name="ck_corporate_event_revision_stage",
        ),
        CheckConstraint("revision_no > 0", name="ck_corporate_event_revision_no"),
        UniqueConstraint("event_id", "revision_no", name="uq_corporate_event_revision"),
        Index("ix_corporate_event_revision_asof", "event_id", desc("known_from")),
        {"comment": "公司事件追加式 revision；后续结果不会覆写首次披露的历史知识。"},
    )

    event_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="公司事件 revision UUID。"
    )
    event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("corporate_event.event_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属公司事件 UUID。",
    )
    stage: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="计划、进展、结果、更正或撤回阶段。"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, comment="来源确认的事件状态。")
    report_period: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="可选关联报告期末。"
    )
    event_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="可选事件发生或计划日期。"
    )
    effective_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="可选事件实际生效日期。"
    )
    primary_document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("disclosure_document.document_id", ondelete="RESTRICT"),
        nullable=False,
        comment="该 revision 的主证据文档 UUID。",
    )


class CorporateEarningsValue(Base):
    """保存业绩预告或快报 `revision` 的指标数值，区间与单值均保留来源表达。

    预告可能只给增长区间、盈亏区间或单点快报，来源表达和单位不能被强行压成一个精确值；上/下界、
    单值、币种和口径各自保存。它不是经审计报表，也不应被财务读取层当作最终披露数值；后续公告
    形成新版本或关联文档，而非原地覆盖。
    """

    __tablename__ = "corporate_earnings_value"
    __table_args__ = (
        CheckConstraint(
            "value_kind IN ('GUIDANCE', 'EXPRESS')", name="ck_corporate_earnings_value_kind"
        ),
        CheckConstraint(
            "value_low IS NULL OR value_high IS NULL OR value_low <= value_high",
            name="ck_corporate_earnings_value_range",
        ),
        {"comment": "公司业绩预告或快报指标；不混入正式财务报表事实。"},
    )

    event_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("corporate_event_revision.event_revision_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属公司事件 revision UUID。",
    )
    metric_code: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False, comment="业绩指标稳定编码。"
    )
    value_low: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露区间下限；单值时可为空。"
    )
    value_high: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露区间上限；单值时可为空。"
    )
    value_single: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露单值；区间表达时可为空。"
    )
    prior_value: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露的同期或上期值；缺失不以其他财报补齐。"
    )
    currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="金额币种 ISO 代码；比率指标时为空。"
    )
    amount_unit: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="金额原始单位或缩放口径。"
    )
    metric_unit: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="金额、每股金额或比例等指标量纲，避免快报指标混算。"
    )
    change_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="来源披露同比或环比变化比例；口径由指标定义。"
    )
    change_ratio_low: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10),
        nullable=True,
        comment="业绩预告来源披露的同比区间下限；单值或未披露时为空。",
    )
    change_ratio_high: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10),
        nullable=True,
        comment="业绩预告来源披露的同比区间上限；单值或未披露时为空。",
    )
    value_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="业绩预告或业绩快报值类型。"
    )
    preliminary_status: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="快报的初步或未经审计状态；预告行为空。"
    )


class RestrictedUnlockLot(Base):
    """保存限售解禁事件的批次行，不能加入公告后收益或最新市值等未来字段。

    每个批次按来源事件、可流通日期、数量、股份类别和可选主体记录，允许同一公告存在多个解禁安排。
    历史事件只使用当时已披露信息；不能把后来股价表现、当前市值、实际减持或最终持股结果回填成
    事件字段，以免污染事件日可用的研究和审计边界。
    """

    __tablename__ = "restricted_unlock_lot"
    __table_args__ = ({"comment": "限售解禁批次；计划和实际数量保留为独立来源字段。"},)

    event_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("corporate_event_revision.event_revision_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属解禁事件 revision UUID。",
    )
    lot_no: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="来源批次序号。"
    )
    holder_name_raw: Mapped[str | None] = mapped_column(
        String(300), nullable=True, comment="来源股东或持有人原文；同名不等同于同主体。"
    )
    planned_qty: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True, comment="计划解禁数量；未披露时为空。"
    )
    actual_qty: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True, comment="实际解禁数量；未形成结果时为空。"
    )
    listing_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="可流通上市日期；未披露时为空。"
    )
    quantity_unit: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="解禁数量单位，通常为股。"
    )
    null_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="计划或实际数量为空的受控原因。"
    )


class ShareCapitalComponent(Base):
    """保存股本事件 `revision` 的股份类别项；分项与总额平衡由质量门验证而不静默修复。

    流通股、限售股、总股本等类别必须以来源定义和单位保存，不能默认所有类别可加总或可跨报告期
    比较。若分项与来源总额不平衡，应记录质量问题或隔离，而不是调整某行使之相等；后续更正通过
    新的事件版本和来源文档表达。
    """

    __tablename__ = "share_capital_component"
    __table_args__ = ({"comment": "股本变动组件；前后数量及变动量均保留来源事实。"},)

    event_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("corporate_event_revision.event_revision_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属股本事件 revision UUID。",
    )
    component_code: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False, comment="股份类别或股本组件稳定编码。"
    )
    before_qty: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True, comment="变动前数量；未披露时为空。"
    )
    after_qty: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True, comment="变动后数量；未披露时为空。"
    )
    change_qty: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True, comment="来源披露的变动数量；未披露时为空。"
    )
    quantity_unit: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="股本数量单位，通常为股。"
    )
    reason_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="来源或方法学解释的变动原因编码。"
    )


class ShareholderHoldingAction(Base):
    """保存股东持股计划或实际变动；主体原文不因同名而在数据库中合并。

    计划与实际执行、增持与减持、权益变动日与公告日都需分别记录；名称相同不代表法律主体相同，
    因此精确身份缺失时保留来源原文。它不根据后续持仓、新闻或交易数据推断执行结果，也不会把
    不同公告的数值合并成“当前持股”。
    """

    __tablename__ = "shareholder_holding_action"
    __table_args__ = (
        CheckConstraint(
            "action_direction IN ('INCREASE', 'DECREASE', 'UNKNOWN')",
            name="ck_shareholder_holding_action_direction",
        ),
        {"comment": "股东持股行动；P2 主体解析只能在有官方身份时另行关联。"},
    )

    event_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("corporate_event_revision.event_revision_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属股东持股事件 revision UUID。",
    )
    actor_name_hash: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        nullable=False,
        comment="来源主体原文的稳定哈希，避免将姓名作为公开身份。",
    )
    action_direction: Mapped[str] = mapped_column(
        String(16), primary_key=True, nullable=False, comment="增持、减持或来源未知方向。"
    )
    actor_name_raw: Mapped[str] = mapped_column(
        String(300), nullable=False, comment="来源主体名称原文。"
    )
    actor_type: Mapped[str | None] = mapped_column(
        String(48), nullable=True, comment="来源披露主体类别；未知时为空。"
    )
    planned_qty: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True, comment="计划变动数量；未披露时为空。"
    )
    actual_qty: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True, comment="实际变动数量；未形成结果时为空。"
    )
    price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="来源披露成交或计划价格；未披露时为空。"
    )
    currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="价格币种；无价格时为空。"
    )
    relation_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="来源披露的主体关系原文。"
    )


class DragonTigerEventRevision(CanonicalRevisionMixin, Base):
    """保存证券日公开交易信息事件 `revision`；同股同日多原因保持多事件。

    上榜原因、市场、交易日和来源事件键共同定位一条公开交易信息，不能只按证券/日期去重。金额、
    换手和席位明细使用来源口径，后续更正追加版本；它不是全部成交明细、投资者身份或收益归因，
    更不能由席位名称猜测最终受益人。
    """

    __tablename__ = "dragon_tiger_event_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_dragon_tiger_event_revision_no"),
        UniqueConstraint(
            "trade_date",
            "security_id",
            "reason_code",
            "source_event_key",
            "revision_no",
            name="uq_dragon_tiger_event_revision",
        ),
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "龙虎榜公开交易事件 revision 父表；同日多原因不合并席位集合。",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="公开交易信息所属交易日。"
    )
    event_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="龙虎榜事件 revision UUID。",
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="上榜永久证券内部键。",
    )
    venue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_venue.venue_id", ondelete="RESTRICT"),
        nullable=False,
        comment="披露该事件的交易场所 UUID。",
    )
    reason_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="受控来源原因编码。"
    )
    reason_raw: Mapped[str] = mapped_column(Text, nullable=False, comment="来源原因原文。")
    reason_family: Mapped[str] = mapped_column(
        String(48), nullable=False, comment="版本化规则映射后的原因家族。"
    )
    close_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="来源同日收盘价；仅在日终披露后可见。"
    )
    turnover_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露成交额；未披露时为空。"
    )
    buy_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露买入额；未披露时为空。"
    )
    sell_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露卖出额；未披露时为空。"
    )
    net_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露净额；未披露时为空。"
    )
    deal_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露买卖榜合计额；不得以市场成交额替代。"
    )
    deal_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="来源披露成交额相关比例，标准化为小数。"
    )
    net_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="来源披露净额相关比例，标准化为小数。"
    )
    turnover_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="来源披露换手比例，标准化为小数。"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, comment="金额币种 ISO 代码。")
    source_event_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True, comment="来源稳定事件键；无键时为空。"
    )


class DragonTigerSeatItem(Base):
    """保存龙虎榜事件内买卖侧前五席位；席位名称不等同于投资者身份。

    买入、卖出、净额和排名属于一条具体公开事件，席位原文可能是营业部、机构或来源展示标签，不能
    映射为个人/机构投资者主数据。席位数量不足、匿名或来源变更要保留事实状态，不能用其他榜单
    补齐；读取时须通过所属事件版本保持同一交易日和来源口径。
    """

    __tablename__ = "dragon_tiger_seat_item"
    __table_args__ = (
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_dragon_tiger_seat_side"),
        CheckConstraint("rank_no BETWEEN 1 AND 5", name="ck_dragon_tiger_seat_rank"),
        ForeignKeyConstraint(
            ["trade_date", "event_revision_id"],
            [
                "dragon_tiger_event_revision.trade_date",
                "dragon_tiger_event_revision.event_revision_id",
            ],
            name="fk_dragon_tiger_seat_event",
            ondelete="RESTRICT",
        ),
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "龙虎榜席位项父表；买卖侧与名次构成事件内稳定键。",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="所属龙虎榜交易日。"
    )
    event_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="所属龙虎榜事件 revision UUID。",
    )
    side: Mapped[str] = mapped_column(
        String(8), primary_key=True, nullable=False, comment="买入或卖出侧。"
    )
    rank_no: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="来源席位排名一至五。"
    )
    seat_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="来源席位代码；缺失时为空。"
    )
    seat_name_raw: Mapped[str] = mapped_column(
        String(300), nullable=False, comment="来源席位名称原文。"
    )
    seat_type: Mapped[str | None] = mapped_column(
        String(48), nullable=True, comment="来源席位类别；未知时为空。"
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 4), nullable=False, comment="该席位买入或卖出金额。"
    )
    ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="来源披露占比；未披露时为空。"
    )
    buy_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源行披露的席位买入额；未披露时为空。"
    )
    sell_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源行披露的席位卖出额；未披露时为空。"
    )
    net_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源行披露的席位净额；未披露时为空。"
    )
    buy_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="来源行披露的席位买入占比，标准化为小数。"
    )
    sell_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="来源行披露的席位卖出占比，标准化为小数。"
    )


class BlockTradeExecutionRevision(CanonicalRevisionMixin, Base):
    """保存一笔大宗交易知识版本；完全相同的经济行通过 `occurrence_no` 保留重数。

    来源可能出现经济字段完全一致的多笔成交，不能用内容哈希或价格/数量键错误去重；发生序号与来源
    事件键共同保留每笔事实。价格、数量、金额、币种、场所和交易日按原始披露保存，更新时新增
    `revision`；它不应由盘后汇总、龙虎榜或行情条目补造。
    """

    __tablename__ = "block_trade_execution_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_block_trade_revision_no"),
        CheckConstraint(
            "price >= 0 AND quantity >= 0 AND amount >= 0", name="ck_block_trade_non_negative"
        ),
        UniqueConstraint(
            "trade_date",
            "release_id",
            "economic_fingerprint",
            "occurrence_no",
            name="uq_block_trade_occurrence",
        ),
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "大宗交易逐笔 revision 父表；相同经济行不被错误去重。",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="大宗交易发生交易日。"
    )
    execution_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="大宗交易 execution revision UUID。",
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="成交永久证券内部键。",
    )
    venue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_venue.venue_id", ondelete="RESTRICT"),
        nullable=False,
        comment="大宗交易场所 UUID。",
    )
    price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, comment="逐笔成交价格。")
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), nullable=False, comment="逐笔成交数量。"
    )
    quantity_unit: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="逐笔成交数量单位。"
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 4), nullable=False, comment="逐笔成交金额。"
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, comment="成交金额币种 ISO 代码。"
    )
    buyer_seat_raw: Mapped[str | None] = mapped_column(
        String(300), nullable=True, comment="买方席位原文；未知时为空。"
    )
    seller_seat_raw: Mapped[str | None] = mapped_column(
        String(300), nullable=True, comment="卖方席位原文；未知时为空。"
    )
    buyer_seat_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="买方来源席位代码；同名席位不据此猜测账户身份。"
    )
    seller_seat_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="卖方来源席位代码；同名席位不据此猜测账户身份。"
    )
    reference_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="可选参考价格；来源未披露时为空。"
    )
    reference_price_type: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="参考价格类型；无参考价时为空。"
    )
    premium_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="来源披露或可证实计算的溢折价比例。"
    )
    source_event_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True, comment="来源逐笔稳定键；无键时为空。"
    )
    source_daily_rank: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="来源页内定位排名；不作为业务排名或投资表现排行。"
    )
    economic_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="无来源键场景保留重数的经济字段摘要。"
    )
    occurrence_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同 release 同经济摘要的来源出现序号。"
    )


class TradingDisclosureReasonMapVersion(Base):
    """保存交易所原因文本到受控原因家族的版本化映射，未知映射显式标为 `UNKNOWN`。

    映射规则本身会随交易所文本和治理口径变化，必须按版本和来源原文保存，不能用应用代码中的
    模糊关键字替代。未知原因保持可见是为了防止错误分类影响榜单统计；新映射只适用于明确选择的
    方法学/版本，不能悄悄重写历史公开事件的解释。
    """

    __tablename__ = "trading_disclosure_reason_map_version"
    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_trading_reason_map_effective_range",
        ),
        ExcludeConstraint(
            ("venue_id", "="),
            ("source_reason_code", "="),
            ("effective_range", "&&"),
            using="gist",
            name="ex_trading_reason_map_time",
        ),
        {"comment": "交易公开信息原因映射版本；规则切换不会改写旧事件原因家族。"},
    )

    reason_map_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="交易原因映射版本 UUID。"
    )
    venue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_venue.venue_id", ondelete="RESTRICT"),
        nullable=False,
        comment="交易所场所 UUID。",
    )
    board: Mapped[str | None] = mapped_column(
        String(48), nullable=True, comment="可选板块范围；全市场规则为空。"
    )
    source_reason_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="来源原因代码。"
    )
    source_reason_text: Mapped[str] = mapped_column(
        Text, nullable=False, comment="来源原因文本原文。"
    )
    canonical_family: Mapped[str] = mapped_column(
        String(48), nullable=False, comment="受控原因家族或 UNKNOWN。"
    )
    rule_ref: Mapped[str] = mapped_column(
        Text, nullable=False, comment="制度规则或方法学证据引用。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="映射规则开始适用日期。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="映射规则结束适用日期；开区间为空。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="原因映射方法学版本 UUID。",
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="规则有效日期半开范围。",
    )
