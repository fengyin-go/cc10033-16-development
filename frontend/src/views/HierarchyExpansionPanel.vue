<template>
  <section class="panel" data-testid="hierarchy-expansion">
    <header class="panel-head">
      <div>
        <h3>层级展开明细</h3>
        <p class="page-desc">按业务日期展开成员路径，查看当日有效路径、适用日期与层级版本。</p>
      </div>
      <div class="panel-actions">
        <label class="inline-field">
          <span>业务日期</span>
          <input v-model="businessDate" type="date" data-testid="hierarchy-business-date" />
        </label>
        <button class="btn primary" data-testid="hierarchy-expand" type="button" @click="loadPaths">
          展开成员路径
        </button>
        <button class="btn" data-testid="hierarchy-publish-version" type="button" @click="publishVersion">
          发布层级版本
        </button>
      </div>
    </header>

    <p v-if="message" class="panel-message" :class="{ 'error-text': failed }">{{ message }}</p>

    <div v-if="versions.length" class="chip-row">
      <span v-for="version in versions" :key="String(version.id)" class="chip">
        {{ version.层级版本 }} · {{ version.构建日期 }} · {{ version.成员数 }} 条关系
      </span>
    </div>

    <table v-if="paths.length" class="data-table">
      <thead>
        <tr>
          <th>成员</th>
          <th>父节点</th>
          <th>当日有效路径</th>
          <th>适用日期</th>
          <th>层级版本</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in paths" :key="row.成员编码">
          <td>{{ row.成员名称 }}（{{ row.成员编码 }}）</td>
          <td>{{ row.父节点名称 || '—' }}</td>
          <td>{{ row.祖先路径文本 }}</td>
          <td>{{ row.路径有效期起 }} ~ {{ row.路径有效期止 }}</td>
          <td>{{ row.层级版本 }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import { loadHierarchyPaths, loadHierarchyVersions, publishHierarchyVersion } from '@/api/backend-service'
import type { HierarchyVersion, PathRecord } from '@/data/types'

const props = defineProps<{ hierarchyName: string }>()

const businessDate = ref('2026-11-20')
const paths = ref<PathRecord[]>([])
const versions = ref<HierarchyVersion[]>([])
const message = ref('')
const failed = ref(false)

async function loadPaths() {
  message.value = ''
  failed.value = false
  try {
    paths.value = await loadHierarchyPaths(props.hierarchyName, businessDate.value)
    versions.value = await loadHierarchyVersions(props.hierarchyName)
    message.value = `已展开 ${paths.value.length} 个成员在 ${businessDate.value} 的当日有效路径`
  } catch (error) {
    failed.value = true
    message.value = error instanceof Error ? error.message : '层级展开失败'
  }
}

async function publishVersion() {
  message.value = ''
  failed.value = false
  try {
    const version = await publishHierarchyVersion(props.hierarchyName, businessDate.value)
    versions.value = await loadHierarchyVersions(props.hierarchyName)
    message.value = `已发布层级版本 ${version.层级版本}（构建日期 ${version.构建日期}）`
  } catch (error) {
    failed.value = true
    message.value = error instanceof Error ? error.message : '层级版本发布失败'
  }
}
</script>
