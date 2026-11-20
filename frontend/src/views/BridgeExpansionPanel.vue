<template>
  <section class="panel" data-testid="bridge-expansion">
    <header class="panel-head">
      <div>
        <h3>桥接分摊明细</h3>
        <p class="page-desc">
          按层级构建交付的父子关系展开事实，登记成员到父节点的分摊结果、当日有效路径和层级版本。
        </p>
      </div>
      <div class="panel-actions">
        <label class="inline-field">
          <span>交付日期</span>
          <input v-model="deliveryDate" type="date" data-testid="bridge-delivery-date" />
        </label>
        <label class="inline-field">
          <span>复算日期</span>
          <input v-model="asOf" type="date" data-testid="bridge-as-of" />
        </label>
        <button class="btn primary" data-testid="bridge-expand" type="button" @click="expand">
          执行桥接展开
        </button>
        <button class="btn" data-testid="bridge-reload" type="button" @click="reloadDetails">
          查看有效明细
        </button>
      </div>
    </header>

    <p v-if="message" class="panel-message" :class="{ 'error-text': failed }">{{ message }}</p>

    <div v-if="result" class="analysis-summary">
      <article class="analysis-card">
        <span class="stat-label">层级版本</span>
        <strong class="stat-value">{{ result.层级版本 }}</strong>
      </article>
      <article class="analysis-card">
        <span class="stat-label">源事实总额</span>
        <strong class="stat-value">{{ result.源事实总额 }}</strong>
      </article>
      <article class="analysis-card">
        <span class="stat-label">分摊总额</span>
        <strong class="stat-value">{{ result.分摊总额 }}</strong>
      </article>
      <article class="analysis-card">
        <span class="stat-label">金额守恒</span>
        <strong class="stat-value">{{ result.金额守恒 ? '是' : '否' }}</strong>
      </article>
    </div>

    <table v-if="result" class="data-table">
      <thead>
        <tr>
          <th>事实ID</th>
          <th>成员</th>
          <th>业务日期</th>
          <th>源金额</th>
          <th>当日有效路径</th>
          <th>父节点分摊</th>
          <th>历史更正</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="fact in result.分摊结果" :key="fact.事实ID">
          <td>{{ fact.事实ID }}</td>
          <td>{{ fact.成员编码 }}</td>
          <td>{{ fact.业务日期 }}</td>
          <td>{{ fact.源金额 }}</td>
          <td>{{ fact.当日有效路径 || '—' }}</td>
          <td>
            <span v-for="share in fact.分摊明细" :key="share.父节点编码" class="chip">
              {{ share.父节点名称 }} {{ share.分摊金额 }}
            </span>
          </td>
          <td>{{ fact.历史更正 ? '是' : '否' }}</td>
        </tr>
      </tbody>
    </table>

    <ul v-if="result?.历史更正.length" class="correction-list" data-testid="correction-list">
      <li v-for="correction in result.历史更正" :key="correction.事实ID">
        <strong>{{ correction.事实ID }}</strong>：{{ correction.说明 }}
      </li>
    </ul>

    <table v-if="details.length" class="data-table" data-testid="expansion-details">
      <thead>
        <tr>
          <th>事实ID</th>
          <th>父节点</th>
          <th>业务日期</th>
          <th>交付日期</th>
          <th>层级版本</th>
          <th>当日有效路径</th>
          <th>分摊金额</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in details" :key="row.明细ID">
          <td>{{ row.事实ID }}</td>
          <td>{{ row.父节点名称 }}（{{ row.父节点编码 }}）</td>
          <td>{{ row.业务日期 }}</td>
          <td>{{ row.交付日期 }}</td>
          <td>{{ row.层级版本 }}</td>
          <td>{{ row.当日有效路径 }}</td>
          <td>{{ row.分摊金额 }}</td>
          <td>{{ row.状态 }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="totals" class="page-foot">
      <span>有效明细合计：源 {{ totals.源事实总额 }} / 分摊 {{ totals.分摊总额 }}</span>
      <span>父节点：{{ JSON.stringify(totals.父节点分摊) }}</span>
    </p>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import { expandBridgeFacts, loadBridgeExpansion } from '@/api/backend-service'
import type { ExpansionDetail, ExpansionResult } from '@/data/types'

const props = defineProps<{ bridgeId: number }>()

const deliveryDate = ref('2026-11-10')
const asOf = ref('2026-11-20')
const result = ref<ExpansionResult | null>(null)
const details = ref<ExpansionDetail[]>([])
const totals = ref<{ 源事实总额: string; 分摊总额: string; 父节点分摊: Record<string, string> } | null>(null)
const message = ref('')
const failed = ref(false)

async function expand() {
  message.value = ''
  failed.value = false
  try {
    const payload = await expandBridgeFacts(props.bridgeId, {
      delivery_date: deliveryDate.value,
      as_of: asOf.value || undefined,
    })
    result.value = payload.result
    message.value = payload.message
    await reloadDetails()
  } catch (error) {
    failed.value = true
    result.value = null
    message.value = error instanceof Error ? error.message : '桥接展开失败'
  }
}

async function reloadDetails() {
  try {
    const payload = await loadBridgeExpansion(props.bridgeId)
    details.value = payload.items
    totals.value = payload.totals
  } catch (error) {
    failed.value = true
    message.value = error instanceof Error ? error.message : '分摊明细读取失败'
  }
}
</script>
