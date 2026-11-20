import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  EntryRow,
  ExpansionDetailsResponse,
  ExpansionResult,
  HierarchyVersion,
  OverviewResult,
  PageResult,
  PathRecord,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

type PathsResponse = { business_date: string; items: PathRecord[]; total: number }

type ExpansionPayload = {
  delivery_date: string
  as_of?: string
  reason?: string
  fact_ids?: string[]
}

export async function loadHierarchyPaths(
  hierarchyName: string,
  businessDate: string,
): Promise<PathRecord[]> {
  const query = new URLSearchParams({ business_date: businessDate }).toString()
  const response = await request(`/api/hierarchy/${encodeURIComponent(hierarchyName)}/paths?${query}`)
  if (!response.ok) throw new Error('成员路径读取失败')
  const payload = (await response.json()) as PathsResponse
  return payload.items
}

export async function loadHierarchyVersions(hierarchyName: string): Promise<HierarchyVersion[]> {
  const response = await request(`/api/hierarchy/${encodeURIComponent(hierarchyName)}/versions`)
  if (!response.ok) throw new Error('层级版本读取失败')
  const payload = (await response.json()) as { items?: HierarchyVersion[] }
  return payload.items ?? []
}

export async function publishHierarchyVersion(
  hierarchyName: string,
  builtOn: string,
): Promise<HierarchyVersion> {
  const query = new URLSearchParams({ built_on: builtOn }).toString()
  const response = await request(`/api/hierarchy/${encodeURIComponent(hierarchyName)}/versions?${query}`, {
    method: 'POST',
  })
  const payload = (await response.json()) as HierarchyVersion & { detail?: unknown }
  if (!response.ok) {
    const detail = payload.detail
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail ?? payload))
  }
  return payload
}

export async function expandBridgeFacts(
  bridgeId: number,
  payload: ExpansionPayload,
): Promise<{ message: string; result: ExpansionResult }> {
  const response = await request(`/api/hierarchy/bridges/${bridgeId}/expansion`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  const body = (await response.json()) as {
    ok?: boolean
    message?: string
    result?: ExpansionResult
    detail?: unknown
  }
  if (!response.ok || !body.result) {
    const detail = body.detail
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail ?? body))
  }
  return { message: body.message ?? '', result: body.result }
}

export async function loadBridgeExpansion(
  bridgeId: number,
  status = '有效',
): Promise<ExpansionDetailsResponse> {
  const query = new URLSearchParams({ status }).toString()
  const response = await request(`/api/hierarchy/bridges/${bridgeId}/expansion?${query}`)
  if (!response.ok) throw new Error('分摊明细读取失败')
  return (await response.json()) as ExpansionDetailsResponse
}
