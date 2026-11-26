import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const CompareView = {
  setup() {
    const flagId = ref(1); const envId = ref(3);
    const detail = ref(null); const matrix = ref(null); const err = ref("");
    async function reload() {
      err.value = "";
      try {
        detail.value = await api(`/api/compare/flag/${flagId.value}/env/${envId.value}`);
        matrix.value = await api("/api/compare/matrix");
      } catch (e) { err.value = e.message || String(e); }
    }
    onMounted(reload);
    return { flagId, envId, detail, matrix, err, reload };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>正式 vs 影子</h2>
      <div class="field"><label>开关ID</label><input type="number" v-model.number="flagId" /></div>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="envId" /></div>
      <button class="btn secondary" @click="reload">对比</button>
      <div class="err" v-if="err">{{ err }}</div>
      <div v-if="detail" style="margin-top:0.8rem">
        <p>
          <strong>对齐裁决：</strong>
          <span class="pill" :class="{'danger': !detail.aligned}">
            {{ detail.aligned ? '已对齐（真绿）' : '漂移/有未升正草案（禁假绿）' }}
          </span>
        </p>
        <p class="hint" v-if="detail.has_pending_shadow">存在 open 影子草案，升正前一律不判已对齐。</p>
        <table v-if="detail.diffs.length">
          <thead><tr><th>字段</th><th>正式</th><th>影子</th></tr></thead>
          <tbody>
            <tr v-for="d in detail.diffs" :key="d.field">
              <td>{{ d.field }}</td><td>{{ d.official }}</td><td>{{ d.shadow }}</td>
            </tr>
          </tbody>
        </table>
        <pre style="margin-top:0.8rem">{{ detail }}</pre>
      </div>
    </div>
    <div class="panel span-7">
      <h2>环境矩阵</h2>
      <div v-if="matrix">
        <table>
          <thead>
            <tr>
              <th>开关</th>
              <th v-for="e in matrix.envs" :key="e.id">{{ e.code }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in matrix.matrix" :key="row.flag_id">
              <td>{{ row.flag_key }}</td>
              <td v-for="c in row.cells" :key="c.env_id">
                <span v-if="c.has_rule">{{ c.percent }}% v{{ c.version }}</span>
                <span v-else class="muted">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>`
};
