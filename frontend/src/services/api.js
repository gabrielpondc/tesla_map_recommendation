async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = Array.isArray(body.detail)
      ? body.detail.map((item) => {
        const field = item.loc?.at(-1)
        const labels = {
          initial_range_km: '起始可用续航', full_range_km: '车辆满电续航',
          reserve_range_km: '基础安全余量', charge_to_percent: '充电目标',
          max_detour_km: '最远下高速距离', lng: '经度', lat: '纬度',
        }
        if (item.type?.includes('parsing') || item.msg === 'Input should be a valid number') {
          return `${labels[field] || '路线参数'}请输入有效数字`
        }
        return labels[field] ? `${labels[field]}：${item.msg}` : item.msg
      }).join('；')
      : body.detail
    throw new Error(detail || '请求失败，请稍后再试')
  }
  return body
}

export const api = {
  config: () => request('/api/config'),
  suggestions: (query) => request(`/api/places/suggest?q=${encodeURIComponent(query)}`),
  reverse: (lng, lat) => request(`/api/places/reverse?lng=${lng}&lat=${lat}`),
  plan: (payload) => request('/api/plan', { method: 'POST', body: JSON.stringify(payload) }),
}
