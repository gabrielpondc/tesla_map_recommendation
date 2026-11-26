function timeoutResult(promise, timeoutMs, fallback) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(fallback), timeoutMs)
    promise.then((value) => {
      clearTimeout(timer)
      resolve(value)
    }, () => {
      clearTimeout(timer)
      resolve(fallback)
    })
  })
}

export function getServerCandidates(config, route, maxDetourKm, mapProvider) {
  if (!config?.baidu_server_enabled) return Promise.resolve([])
  const request = fetch('/api/map/baidu/candidates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      route,
      max_detour_km: maxDetourKm,
      map_provider: mapProvider,
    }),
  }).then(async (response) => {
    if (!response.ok) return []
    const candidates = await response.json()
    return Array.isArray(candidates) ? candidates : []
  }).catch(() => [])
  return timeoutResult(request, 42000, [])
}
