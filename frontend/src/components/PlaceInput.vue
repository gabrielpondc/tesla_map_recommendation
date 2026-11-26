<script setup>
import { ref, watch, onBeforeUnmount } from 'vue'
import { api } from '../services/api'
import UiIcon from './UiIcon.vue'

const props = defineProps({
  modelValue: { type: Object, required: true },
  placeholder: { type: String, default: '输入地点' },
  marker: { type: String, default: 'start' },
  config: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue'])

const text = ref(props.modelValue?.name || '')
const suggestions = ref([])
const open = ref(false)
const loading = ref(false)
let timer

watch(() => props.modelValue?.name, (value) => {
  if (value && value !== text.value) text.value = value
})

function search() {
  emit('update:modelValue', { name: text.value, address: '', lng: null, lat: null })
  clearTimeout(timer)
  if (text.value.trim().length < 2) {
    suggestions.value = []
    return
  }
  timer = setTimeout(async () => {
    loading.value = true
    try {
      const query = text.value.trim()
      const serverSuggestions = await api.suggestions(query)
      const unique = new Map()
      for (const item of serverSuggestions) {
        unique.set(`${item.name}-${item.lng}-${item.lat}`, item)
      }
      suggestions.value = [...unique.values()].slice(0, 8)
      open.value = true
    } catch {
      suggestions.value = []
    } finally {
      loading.value = false
    }
  }, 260)
}

function choose(place) {
  text.value = place.name
  emit('update:modelValue', {
    name: place.name,
    address: place.address || place.district || '',
    lng: place.lng,
    lat: place.lat,
    ...(place._map_provider ? { _map_provider: place._map_provider } : {}),
  })
  open.value = false
}

function blurLater() {
  setTimeout(() => { open.value = false }, 160)
}

onBeforeUnmount(() => clearTimeout(timer))
</script>

<template>
  <div class="place-input">
    <span class="place-marker" :class="marker"><i /></span>
    <div class="place-copy">
      <input
        v-model="text"
        :placeholder="placeholder"
        @input="search"
        @focus="open = suggestions.length > 0"
        @blur="blurLater"
      />
      <span v-if="modelValue.address" class="place-address">{{ modelValue.address }}</span>
    </div>
    <span v-if="loading" class="input-spinner" />
    <div v-if="open" class="suggestions">
      <button v-for="item in suggestions" :key="`${item.name}-${item.lng}`" type="button" @mousedown.prevent="choose(item)">
        <span class="suggestion-pin"><UiIcon name="pin" /></span>
        <span><strong>{{ item.name }}</strong><small>{{ item.address || item.district }}</small></span>
      </button>
    </div>
  </div>
</template>
