import { getAmapCandidatesForBaiduRoute } from './amap'
import { getServerCandidates } from './serverCandidates'

let sdkPromise
const roundTripCache = new Map()
let localCityPromise

export async function loadBaidu(config) {
  if (!config?.baidu_enabled || !config?.baidu_ak) throw new Error('百度地图 AK 未配置')
  if (window.BMap) return window.BMap
  if (!sdkPromise) {
    sdkPromise = new Promise((resolve, reject) => {
      const callbackName = `__diantuBaiduReady${Date.now()}`
      const script = document.createElement('script')
      const timer = setTimeout(() => reject(new Error('百度地图加载超时')), 15000)
      window[callbackName] = () => {
        clearTimeout(timer)
        delete window[callbackName]
        resolve(window.BMap)
      }
      script.src = `https://api.map.baidu.com/api?v=3.0&ak=${encodeURIComponent(config.baidu_ak)}&callback=${callbackName}`
      script.onerror = () => {
        clearTimeout(timer)
        reject(new Error('百度地图加载失败'))
      }
      document.head.appendChild(script)
    })
  }
  return sdkPromise
}

function pointObject(raw) {
  if (!raw) return null
  const lng = Number(raw.lng)
  const lat = Number(raw.lat)
  return Number.isFinite(lng) && Number.isFinite(lat) ? { lng, lat } : null
}

function haversineKm(a, b) {
  const radius = 6371.0088
  const lat1 = a.lat * Math.PI / 180
  const lat2 = b.lat * Math.PI / 180
  const dLat = lat2 - lat1
  const dLng = (b.lng - a.lng) * Math.PI / 180
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2
  return 2 * radius * Math.asin(Math.sqrt(h))
}

function nearestRouteDistance(point, polyline) {
  let best = Infinity
  const stride = Math.max(1, Math.floor(polyline.length / 180))
  for (let index = 0; index < polyline.length; index += stride) {
    best = Math.min(best, haversineKm(point, polyline[index]))
  }
  return best
}

function isTeslaSuperchargerName(name) {
  return /特斯拉.*超级充电|tesla.*supercharger/i.test(name || '')
}

async function toBaiduPoint(BMap, place) {
  const point = new BMap.Point(place.lng, place.lat)
  if (place?._map_provider === 'baidu' || place?.source?.startsWith('baidu_')) return point
  if (!BMap.Convertor) return point
  return new Promise((resolve) => {
    const convertor = new BMap.Convertor()
    convertor.translate([point], 3, 5, (result) => {
      resolve(result.status === 0 && result.points?.[0] ? result.points[0] : point)
    })
  })
}

function numericValue(value) {
  if (Number.isFinite(Number(value))) return Number(value)
  const parsed = Number.parseFloat(String(value || '').replace(/[^0-9.]/g, ''))
  return Number.isFinite(parsed) ? parsed : 0
}

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

export async function getBaiduDrivingRoute(config, origin, destination, waypoints = []) {
  const BMap = await loadBaidu(config)
  const [start, end, via] = await Promise.all([
    toBaiduPoint(BMap, origin),
    toBaiduPoint(BMap, destination),
    Promise.all(waypoints.map((point) => toBaiduPoint(BMap, point))),
  ])
  return new Promise((resolve, reject) => {
    let driving
    let settled = false
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true
        reject(new Error('百度驾车路线请求超时'))
      }
    }, 15000)
    const onSearchComplete = (results) => {
      if (settled) return
      if (driving.getStatus() !== window.BMAP_STATUS_SUCCESS || !results?.getPlan?.(0)) {
        settled = true
        clearTimeout(timer)
        reject(new Error('百度未返回可用的驾车路线'))
        return
      }
      const plan = results.getPlan(0)
      const rawPath = []
      for (let index = 0; index < plan.getNumRoutes(); index += 1) {
        for (const raw of plan.getRoute(index).getPath() || []) {
          const point = pointObject(raw)
          if (point && (!rawPath.length || point.lng !== rawPath.at(-1).lng || point.lat !== rawPath.at(-1).lat)) {
            rawPath.push(point)
          }
        }
      }
      if (rawPath.length < 2) {
        settled = true
        clearTimeout(timer)
        reject(new Error('百度道路折线为空'))
        return
      }
      const polyline = rawPath.filter((_, index) => index === 0 || index === rawPath.length - 1 || index % 2 === 0)
      const distanceKm = Math.round(numericValue(plan.getDistance(false)) / 100) / 10
      const durationMin = Math.max(1, Math.round(numericValue(plan.getDuration(false)) / 60))
      if (!Number.isFinite(distanceKm) || distanceKm <= 0 || !Number.isFinite(durationMin)) {
        settled = true
        clearTimeout(timer)
        reject(new Error('百度返回的路线距离无效'))
        return
      }
      settled = true
      clearTimeout(timer)
      resolve({
        distance_km: distanceKm,
        duration_min: durationMin,
        tolls_yuan: 0,
        polyline,
      })
    }
    driving = new BMap.DrivingRoute('全国', {
      policy: window.BMAP_DRIVING_POLICY_LEAST_TIME,
      onSearchComplete,
    })
    driving.search(start, end, via.length ? { waypoints: via } : undefined)
  })
}

async function localSearch(BMap, keyword, center = null, radiusMeters = 0) {
  return new Promise((resolve) => {
    let search
    let settled = false
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true
        resolve([])
      }
    }, 8000)
    const onSearchComplete = (results) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (search.getStatus() !== window.BMAP_STATUS_SUCCESS || !results) {
        resolve([])
        return
      }
      const pois = []
      for (let index = 0; index < results.getCurrentNumPois(); index += 1) {
        const poi = results.getPoi(index)
        const point = pointObject(poi?.point)
        if (!point) continue
        pois.push({
          id: String(poi.uid || `${poi.title}-${point.lng}-${point.lat}`),
          name: poi.title || keyword,
          address: poi.address || '',
          ...point,
        })
      }
      resolve(pois)
    }
    search = new BMap.LocalSearch(center || '全国', { pageCapacity: 50, onSearchComplete })
    if (center && typeof center.lng !== 'undefined' && typeof center.lat !== 'undefined' && radiusMeters > 0) {
      search.searchNearby(keyword, center, radiusMeters)
    }
    else search.search(keyword)
  })
}

function getLocalCity(BMap) {
  if (!localCityPromise) {
    localCityPromise = new Promise((resolve) => {
      const timer = setTimeout(() => resolve(''), 4000)
      const service = new BMap.LocalCity()
      service.get((result) => {
        clearTimeout(timer)
        resolve(result?.name || '')
      })
    })
  }
  return localCityPromise
}

function geocodeSuggestion(BMap, keywords, city = '') {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), 5000)
    const geocoder = new BMap.Geocoder()
    geocoder.getPoint(keywords, (rawPoint) => {
      clearTimeout(timer)
      const point = pointObject(rawPoint)
      resolve(point ? {
        id: `geocode-${keywords}-${point.lng}-${point.lat}`,
        name: keywords,
        address: city || '百度地图匹配地点',
        ...point,
      } : null)
    }, city || undefined)
  })
}

async function mapWithTimeBudget(items, concurrency, budgetMs, worker) {
  const results = []
  const deadline = Date.now() + budgetMs
  let cursor = 0
  const runners = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (cursor < items.length && Date.now() < deadline) {
      const index = cursor
      cursor += 1
      const remaining = Math.max(1, deadline - Date.now())
      const result = await timeoutResult(Promise.resolve(worker(items[index], index)), remaining, null)
      if (result != null) results.push(result)
    }
  })
  await Promise.all(runners)
  return results
}

async function enrichRoundTrip(config, BMap, candidate, maxDetourKm) {
  const radiusKm = Math.max(30, maxDetourKm)
  const cacheKey = `${candidate.id}:${radiusKm}`
  if (roundTripCache.has(cacheKey)) return { ...candidate, ...roundTripCache.get(cacheKey) }
  const center = new BMap.Point(candidate.lng, candidate.lat)
  const [tolls, exits, entries] = await Promise.all([
    localSearch(BMap, '收费站', center, radiusKm * 1000),
    localSearch(BMap, '高速出口', center, radiusKm * 1000),
    localSearch(BMap, '高速入口', center, radiusKm * 1000),
  ])
  function uniquePoints(pois, pattern) {
    const unique = new Map()
    for (const poi of pois) {
      if (!pattern.test(poi.name)) continue
      unique.set(poi.id, poi)
    }
    return [...unique.values()]
  }
  const exitPoints = uniquePoints([...tolls, ...exits], /收费站|出口/)
  const entryPoints = uniquePoints([...tolls, ...entries], /收费站|入口/)
  const nearestExits = (exitPoints.length ? exitPoints : entryPoints)
    .sort((a, b) => haversineKm(a, candidate) - haversineKm(b, candidate)).slice(0, 3)
  const nearestEntries = (entryPoints.length ? entryPoints : exitPoints)
    .sort((a, b) => haversineKm(a, candidate) - haversineKm(b, candidate)).slice(0, 3)
  const [exitResults, entryResults] = await Promise.all([
    Promise.allSettled(nearestExits.map(async (roadPoint) => ({
      roadPoint, route: await getBaiduDrivingRoute(config, { ...roadPoint, _map_provider: 'baidu' }, candidate),
    }))),
    Promise.allSettled(nearestEntries.map(async (roadPoint) => ({
      roadPoint, route: await getBaiduDrivingRoute(config, candidate, { ...roadPoint, _map_provider: 'baidu' }),
    }))),
  ])
  const validExits = exitResults.filter((item) => item.status === 'fulfilled').map((item) => item.value)
    .sort((a, b) => a.route.distance_km - b.route.distance_km)
  const validEntries = entryResults.filter((item) => item.status === 'fulfilled').map((item) => item.value)
    .sort((a, b) => a.route.distance_km - b.route.distance_km)
  let bestExit = validExits[0]
  let bestEntry = validEntries[0]
  if (bestExit && !bestEntry) {
    try {
      bestEntry = {
        roadPoint: bestExit.roadPoint,
        route: await getBaiduDrivingRoute(config, candidate, { ...bestExit.roadPoint, _map_provider: 'baidu' }),
      }
    } catch { /* keep one-way data */ }
  }
  const data = {
    ...(bestExit ? {
      exit_name: bestExit.roadPoint.name,
      exit_distance_km: bestExit.route.distance_km,
      exit_duration_min: bestExit.route.duration_min,
      distance_to_route_km: bestExit.route.distance_km,
    } : {}),
    ...(bestEntry ? {
      entry_name: bestEntry.roadPoint.name,
      entry_distance_km: bestEntry.route.distance_km,
      entry_duration_min: bestEntry.route.duration_min,
    } : {}),
  }
  roundTripCache.set(cacheKey, data)
  return { ...candidate, ...data }
}

export async function getBaiduPlanningData(config, origin, destination, maxDetourKm) {
  const BMap = await loadBaidu(config)
  const route = await getBaiduDrivingRoute(config, origin, destination)
  const serverCandidates = await getServerCandidates(config, route, maxDetourKm, 'baidu')
  if (serverCandidates.length) return { route, candidates: serverCandidates }
  // Keep a guaranteed set of searches spread over the whole route. The old
  // triplet-per-point queue could exhaust its time budget near the origin and
  // never inspect the later part of a long journey.
  const sampleCount = Math.min(24, Math.max(4, Math.ceil(route.distance_km / 80)))
  const indices = [...new Set(Array.from({ length: sampleCount + 1 }, (_, index) =>
    Math.round(index * (route.polyline.length - 1) / sampleCount),
  ))]
  const spacingKm = route.distance_km / sampleCount
  const radius = Math.min(100000, Math.max(
    35000,
    (spacingKm * 0.65 + Math.max(maxDetourKm, 20)) * 1000,
  ))
  const routePoints = indices.map((index) => new BMap.Point(
    route.polyline[index].lng, route.polyline[index].lat,
  ))
  // "充电站" is the coverage pass and must visit every part of the route.
  // Tesla/service-area queries are supplemental and no longer block coverage.
  const coverageTasks = routePoints.map((center) => ({
    center, radius, keyword: '充电站', type: 'charging_station',
  }))
  const supplementalTasks = []
  for (const center of routePoints) {
    supplementalTasks.push({ center, radius, keyword: '特斯拉超级充电站', type: 'tesla_supercharger' })
    supplementalTasks.push({ center, radius, keyword: '服务区充电站', type: 'service_area' })
  }
  const coverageGroups = await mapWithTimeBudget(coverageTasks, 16, 16000, async (task) => ({
    pois: await localSearch(BMap, task.keyword, task.center, task.radius), type: task.type,
  }))
  const supplementalGroups = await mapWithTimeBudget(supplementalTasks, 16, 10000, async (task) => ({
    pois: await localSearch(BMap, task.keyword, task.center, task.radius), type: task.type,
  }))
  const groups = [...coverageGroups, ...supplementalGroups]
  const seen = new Set()
  const candidates = []
  for (const group of groups) {
    for (const poi of group.pois) {
      if (seen.has(poi.id)) continue
      seen.add(poi.id)
      let stationType = 'charging_station'
      if (isTeslaSuperchargerName(poi.name)) stationType = 'tesla_supercharger'
      else if (poi.name.includes('服务区')) stationType = 'service_area'
      candidates.push({
        ...poi,
        station_type: stationType,
        progress_km: 0,
        distance_to_route_km: 0,
        source: 'baidu_js',
        _map_provider: 'baidu',
      })
    }
  }
  const corridor = candidates.map((candidate) => ({
    ...candidate, _routeDistance: nearestRouteDistance(candidate, route.polyline),
  }))
  // Keep a wider real-station fallback pool. The planner still tries the
  // user's requested detour first and only widens it when a route gap needs it.
  const service = corridor.filter((item) => item.station_type === 'service_area' && item._routeDistance <= 50)
  const general = corridor.filter((item) => item.station_type === 'charging_station' && item._routeDistance <= 50)
  const tesla = corridor.filter((item) => item.station_type === 'tesla_supercharger')
    .sort((a, b) => a._routeDistance - b._routeDistance)
  const enrichedTesla = await mapWithTimeBudget(tesla, 5, 26000, async (candidate) =>
    timeoutResult(enrichRoundTrip(config, BMap, candidate, maxDetourKm), 24000, candidate))
  const enrichedIds = new Set(enrichedTesla.map((candidate) => candidate.id))
  const remainingTesla = tesla.filter((candidate) => !enrichedIds.has(candidate.id))
  const baiduCandidates = [...service, ...general, ...enrichedTesla, ...remainingTesla]
    .map(({ _routeDistance, _map_provider, ...item }) => item)
    .filter((item) => Number.isFinite(item.lng) && Number.isFinite(item.lat))
  const minimumCoverage = Math.max(4, Math.ceil(route.distance_km / 250))
  const amapFallback = baiduCandidates.length < minimumCoverage && config?.amap_enabled
    ? await timeoutResult(
      getAmapCandidatesForBaiduRoute(config, route, maxDetourKm),
      26000,
      [],
    )
    : []
  const merged = new Map()
  for (const candidate of [...baiduCandidates, ...amapFallback]) merged.set(candidate.id, candidate)
  return { route, candidates: [...merged.values()] }
}

export async function getBaiduSuggestions(config, keywords) {
  const BMap = await loadBaidu(config)
  const query = keywords.trim()
  const nationwide = await localSearch(BMap, query)
  const text = query.replace(/\s+/g, '')
  const explicitRegion = text.match(/^(.{2,6}?(?:市|区|县|州))/)?.[1] || ''
  const shortPrefix = /^[\u4e00-\u9fff]{2,}/.test(text) ? text.slice(0, Math.min(3, text.length)) : ''
  const localCity = await getLocalCity(BMap)
  const regions = [...new Set([
    explicitRegion,
    shortPrefix,
    shortPrefix && !shortPrefix.endsWith('市') ? `${shortPrefix}市` : '',
    text.length <= 4 ? text : '',
    text.length <= 4 && !text.endsWith('市') ? `${text}市` : '',
    localCity,
  ].filter(Boolean))].slice(0, 5)
  const regionalGroups = nationwide.length >= 8
    ? []
    : await Promise.all(regions.map((region) => localSearch(BMap, query, region)))
  const merged = [...nationwide, ...regionalGroups.flat()]
  if (!merged.length) {
    const geocoded = await timeoutResult(
      Promise.all(regions.slice(0, 3).map((region) => geocodeSuggestion(BMap, query, region))),
      6000,
      [],
    )
    merged.push(...geocoded.filter(Boolean))
  }
  const unique = new Map()
  for (const poi of merged) {
    const key = poi.id || `${poi.name}-${poi.lng}-${poi.lat}`
    if (!unique.has(key)) unique.set(key, poi)
  }
  return [...unique.values()].slice(0, 8)
    .map((poi) => ({ ...poi, district: '', _map_provider: 'baidu' }))
}

export async function locateWithBaidu(config) {
  const BMap = await loadBaidu(config)
  return new Promise((resolve, reject) => {
    const service = new BMap.Geolocation()
    service.getCurrentPosition(function callback(result) {
      if (this.getStatus() !== window.BMAP_STATUS_SUCCESS || !result?.point) {
        reject(new Error('百度定位失败'))
        return
      }
      resolve({
        name: result.address?.city ? `${result.address.city}${result.address.district || ''}` : '我的位置',
        address: result.address?.street || '百度定位',
        lng: result.point.lng,
        lat: result.point.lat,
        _map_provider: 'baidu',
      })
    }, { enableHighAccuracy: true })
  })
}
