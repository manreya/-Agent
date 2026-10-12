import { createRouter, createWebHistory } from 'vue-router'
import Intake from '@/views/Intake.vue'
import Generating from '@/views/Generating.vue'
import Approval from '@/views/Approval.vue'
import FinalPlan from '@/views/FinalPlan.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'Intake', component: Intake },
    { path: '/generating', name: 'Generating', component: Generating },
    { path: '/approval', name: 'Approval', component: Approval },
    { path: '/plan', name: 'FinalPlan', component: FinalPlan },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

export default router
