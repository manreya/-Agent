<template>
  <div id="app">
    <a-layout style="min-height: 100vh">
      <a-layout-header style="background: #0f3d2e; padding: 0 50px; display: flex; align-items: center; justify-content: space-between">
        <router-link to="/" style="color: white; font-size: 22px; font-weight: bold; text-decoration: none">
          🎯 团建活动策划 Agent
        </router-link>
        <a-tag v-if="store.teamId" color="green" style="margin-left: 12px">
          团队: {{ store.teamId }}
        </a-tag>
      </a-layout-header>
      <a-layout-content style="padding: 24px; max-width: 1200px; width: 100%; margin: 0 auto">
        <router-view />
      </a-layout-content>
      <a-layout-footer style="text-align: center; color: #888">
        团建活动策划 Agent · FastAPI + LangGraph · HITL 人工审批
      </a-layout-footer>
    </a-layout>
  </div>
</template>

<script setup lang="ts">
import { useEventFlowStore } from '@/stores/eventFlow'

const store = useEventFlowStore()
</script>

<style>
#app {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial,
    'Noto Sans', sans-serif;
}
</style>
