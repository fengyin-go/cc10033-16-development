/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

export type PathRecord = {
  层级名称: string
  成员编码: string
  成员名称: string
  父节点编码: string
  父节点名称: string
  祖先路径: string[]
  祖先路径文本: string
  祖先路径键: string
  层级深度: number
  适用日期: string
  路径有效期起: string
  路径有效期止: string
  层级版本: string
  版本来源: string
  版本记录ID: number | null
}

export type ParentShare = {
  父节点编码: string
  父节点名称: string
  分配权重: string
  分摊金额: string
  尾差调整?: string
}

export type CorrectionRecord = {
  事实ID: string
  业务日期: string
  交付日期: string
  原层级版本: string
  新层级版本: string
  原父节点分摊: Record<string, { 父节点名称: string; 分摊金额: string }>
  新父节点分摊: Record<string, { 父节点名称: string; 分摊金额: string }>
  当日有效路径: string
  交付日路径: string
  说明: string
}

export type FactAllocation = {
  事实ID: string
  成员编码: string
  源金额: string
  分摊合计: string
  金额守恒: boolean
  权重来源: string
  权重说明: string
  重复路径: { 成员编码: string; 父节点编码: string; 权重: string }[]
  业务日期: string
  交付日期: string
  层级版本: string
  当日有效路径: string
  交付日路径: string
  历史更正: boolean
  更正说明?: string
  分摊明细: ParentShare[]
}

export type ExpansionResult = {
  桥接名称: string
  交付日期: string
  复算日期: string
  复算批次: string
  层级版本: string
  事实笔数: number
  明细笔数: number
  替代明细: string[]
  历史更正: CorrectionRecord[]
  源事实总额: string
  分摊总额: string
  父节点分摊: Record<string, string>
  版本分摊: Record<string, string>
  金额守恒: boolean
  分摊结果: FactAllocation[]
}

export type ExpansionDetail = {
  id: number
  明细ID: string
  桥接名称: string
  事实ID: string
  成员编码: string
  成员名称: string
  父节点编码: string
  父节点名称: string
  层级版本: string
  业务日期: string
  交付日期: string
  当日有效路径: string
  分配权重: string
  源金额: string
  分摊金额: string
  历史更正: string
  状态: string
}

export type ExpansionDetailsResponse = {
  bridge_name: string
  items: ExpansionDetail[]
  total: number
  totals: {
    源事实总额: string
    分摊总额: string
    金额守恒: boolean
    父节点分摊: Record<string, string>
    版本分摊: Record<string, string>
  }
}


export type HierarchyVersion = {
  id: number
  层级名称: string
  层级版本: string
  构建日期: string
  构建类型: string
  成员数: number
  根节点: string[]
  status: string
}
