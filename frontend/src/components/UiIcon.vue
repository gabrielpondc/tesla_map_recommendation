<script setup>
defineProps({ name: { type: String, required: true } })
</script>

<template>
  <svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
    <template v-if="name === 'route'">
      <circle cx="6" cy="18" r="2" /><circle cx="18" cy="6" r="2" /><path d="M8 18h3a3 3 0 0 0 3-3V9a3 3 0 0 1 3-3" />
    </template>
    <template v-else-if="name === 'help'">
      <circle cx="12" cy="12" r="9" /><path d="M9.8 9a2.4 2.4 0 1 1 3.6 2.1c-.9.5-1.4 1.1-1.4 2.1" /><path d="M12 17h.01" />
    </template>
    <template v-else-if="name === 'locate'">
      <circle cx="12" cy="12" r="3" /><circle cx="12" cy="12" r="7" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
    </template>
    <template v-else-if="name === 'swap'">
      <path d="M7 4v15M4 7l3-3 3 3M17 20V5M14 17l3 3 3-3" />
    </template>
    <template v-else-if="name === 'shield'">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /><path d="m9 12 2 2 4-4" />
    </template>
    <template v-else-if="name === 'bolt'">
      <path d="m13 2-9 12h8l-1 8 9-12h-8l1-8z" />
    </template>
    <template v-else-if="name === 'alert'">
      <circle cx="12" cy="12" r="9" /><path d="M12 7v6M12 17h.01" />
    </template>
    <template v-else-if="name === 'arrow-up-right'">
      <path d="M7 17 17 7M8 7h9v9" />
    </template>
    <template v-else-if="name === 'arrow-left'">
      <path d="m15 18-6-6 6-6M9 12h10" />
    </template>
    <template v-else-if="name === 'arrow-right'">
      <path d="m9 6 6 6-6 6M5 12h10" />
    </template>
    <template v-else-if="name === 'share'">
      <circle cx="18" cy="5" r="2" /><circle cx="6" cy="12" r="2" /><circle cx="18" cy="19" r="2" /><path d="m8 11 8-5M8 13l8 5" />
    </template>
    <template v-else-if="name === 'pin'">
      <path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0z" /><circle cx="12" cy="10" r="2.5" />
    </template>
    <template v-else-if="name === 'flag'">
      <path d="M5 22V4M5 5h10l-1 4 1 4H5" />
    </template>
    <template v-else-if="name === 'info'">
      <circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 7h.01" />
    </template>
    <template v-else-if="name === 'check'">
      <path d="m5 12 4 4L19 6" />
    </template>
  </svg>
</template>
