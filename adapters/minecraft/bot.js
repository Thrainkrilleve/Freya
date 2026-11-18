'use strict'

// Companion for one loopback Minecraft Java server.
// Speaks JSON lines on stdin/stdout. Logs go to stderr so they cannot
// be mistaken for a result. This program does not import the assistant,
// does not listen on a port, and does not write account files.
// Offline sign-in is the default. Microsoft sign-in is optional and
// uses the library's own cache outside this repository.
//
// Reflex numbers stay in step with the Python reflex module:
// eat at food 6, flee at health 6, attack within 2.5, dodge within 6.

console.log = (...parts) => {
  console.error(...parts)
}

const EAT_AT = 6
const FLEE_AT = 6
const ATTACK_WITHIN = 2.5
const DODGE_WITHIN = 6
const FOOD = new Set([
  'bread', 'cooked_beef', 'cooked_porkchop', 'cooked_chicken',
  'cooked_mutton', 'cooked_rabbit', 'baked_potato', 'apple',
  'carrot', 'golden_carrot', 'sweet_berries', 'beetroot',
  'melon_slice', 'cookie', 'pumpkin_pie', 'beef', 'porkchop',
  'chicken', 'mutton', 'rabbit', 'potato', 'rotten_flesh',
])
const HOSTILE = new Set([
  'zombie', 'skeleton', 'creeper', 'spider', 'cave_spider', 'enderman',
  'witch', 'slime', 'drowned', 'husk', 'stray', 'pillager', 'vindicator',
  'blaze', 'ghast', 'phantom', 'warden', 'piglin', 'hoglin', 'zoglin',
  'silverfish', 'endermite', 'magma_cube', 'guardian', 'shulker',
])
const FAILED = 'The action failed.'
const UNKNOWN = 'That action is not one this adapter performs.'
const NO_WORLD = 'The bot is not in a world.'
const STOPPED = 'Play has stopped.'
const SLOTS = {
  hand: 'hand',
  offhand: 'off-hand',
  head: 'head',
  torso: 'torso',
  legs: 'legs',
  feet: 'feet',
}

function isLoopback (host) {
  const text = String(host || '').trim().toLowerCase()
  if (text === 'localhost' || text === '::1') return true
  const parts = text.split('.')
  if (parts.length !== 4 || parts[0] !== '127') return false
  return parts.every((part) => /^\d+$/.test(part) && Number(part) <= 255)
}

function readConfig () {
  const host = process.env.FREYA_MINECRAFT_HOST || ''
  const port = Number(process.env.FREYA_MINECRAFT_PORT || '25565')
  const username = process.env.FREYA_MINECRAFT_USERNAME || 'Freya'
  const auth = String(process.env.FREYA_MINECRAFT_AUTH || 'offline').trim().toLowerCase()
  const version = process.env.FREYA_MINECRAFT_VERSION || ''
  if (!isLoopback(host)) throw new Error('host')
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('port')
  if (!/^[A-Za-z0-9_]{1,16}$/.test(username)) throw new Error('name')
  if (auth !== 'offline' && auth !== 'microsoft') throw new Error('auth')
  if (version && !/^[0-9]+\.[0-9]+(\.[0-9]+)?$/.test(version)) throw new Error('version')
  return { host, port, username, auth, version }
}

function reply (message) {
  process.stdout.write(JSON.stringify(message) + '\n')
}

function sleep (ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function withTimeout (promise, ms) {
  let timer
  const limit = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('timeout')), ms)
  })
  return Promise.race([promise, limit]).finally(() => clearTimeout(timer))
}

function bare (name) {
  return String(name || '').split(':').pop()
}

let config
try {
  config = readConfig()
} catch (err) {
  console.error('The target was refused.')
  process.exit(1)
}

const mineflayer = require('mineflayer')
const pathfinderMod = require('mineflayer-pathfinder')
const pvpMod = require('mineflayer-pvp')
const collectMod = require('mineflayer-collectblock')
const toolMod = require('mineflayer-tool')
const armorMod = require('mineflayer-armor-manager')
const Vec3 = require('vec3')

const options = {
  host: config.host,
  port: config.port,
  username: config.username,
  auth: config.auth,
  hideErrors: true,
}
if (config.version) options.version = config.version

const bot = mineflayer.createBot(options)
bot.loadPlugin(pathfinderMod.pathfinder)
bot.loadPlugin(pvpMod.plugin)
bot.loadPlugin(collectMod.plugin)
if (toolMod.plugin) bot.loadPlugin(toolMod.plugin)
bot.loadPlugin(typeof armorMod === 'function' ? armorMod : armorMod.plugin)

const goals = pathfinderMod.goals
const Movements = pathfinderMod.Movements

let spawned = false
let stopped = false
let busy = false
let generation = 0
let dodge = 'left'
let stuckKey = ''
let stuck = 0
let chest = null
const recentChat = []

function pluginOfGoal () {
  return bot.pathfinder
}

function haltMotion () {
  generation += 1
  closeChest()
  try {
    if (pluginOfGoal()) pluginOfGoal().setGoal(null)
  } catch (err) { /* already clear */ }
  try {
    if (bot.pvp) bot.pvp.stop()
  } catch (err) { /* already clear */ }
  try {
    bot.clearControlStates()
  } catch (err) { /* already clear */ }
  try {
    bot.stopDigging()
  } catch (err) { /* already clear */ }
  try {
    if (bot.collectBlock && bot.collectBlock.cancelTask) bot.collectBlock.cancelTask()
  } catch (err) { /* already clear */ }
}

function closeChest () {
  if (!chest) return
  try { chest.close() } catch (err) { /* already shut */ }
  chest = null
}

function shutdown () {
  if (stopped) return
  stopped = true
  haltMotion()
  try { bot.quit() } catch (err) { /* already gone */ }
  setTimeout(() => process.exit(0), 50)
}

function mc () {
  return require('minecraft-data')(bot.version)
}

function inWorld () {
  return spawned && bot.entity && !stopped
}

function stackNamed (name) {
  const wanted = bare(name)
  return bot.inventory.items().find((item) => item.name === wanted) || null
}

function findBlockName (name, maxDistance) {
  const data = mc()
  const info = data.blocksByName[bare(name)]
  if (!info) return null
  return bot.findBlock({ matching: info.id, maxDistance: maxDistance || 64 })
}

function entityNamed (name) {
  const wanted = bare(name)
  let best = null
  let bestDistance = Infinity
  for (const entity of Object.values(bot.entities)) {
    if (!entity || entity === bot.entity || !entity.position) continue
    const ename = entity.username || entity.name || ''
    if (ename !== wanted && bare(ename) !== wanted) continue
    const distance = bot.entity.position.distanceTo(entity.position)
    if (distance < bestDistance) {
      best = entity
      bestDistance = distance
    }
  }
  return best
}

function nearestHostile () {
  let best = null
  let bestDistance = Infinity
  for (const entity of Object.values(bot.entities)) {
    if (!entity || entity === bot.entity || !entity.position) continue
    if (!HOSTILE.has(entity.name)) continue
    const distance = bot.entity.position.distanceTo(entity.position)
    if (distance < bestDistance) {
      best = entity
      bestDistance = distance
    }
  }
  return best
}

async function gotoAction (args) {
  if (!pluginOfGoal()) throw new Error('fail')
  if (args.player) {
    const player = bot.players[args.player]
    if (!player || !player.entity) throw new Error('fail')
    const pos = player.entity.position
    await withTimeout(pluginOfGoal().goto(new goals.GoalNear(pos.x, pos.y, pos.z, 2)), 20000)
    return
  }
  if (args.block) {
    const block = findBlockName(args.block, 128)
    if (!block) throw new Error('fail')
    const pos = block.position
    await withTimeout(pluginOfGoal().goto(new goals.GoalNear(pos.x, pos.y, pos.z, 1)), 20000)
    return
  }
  await withTimeout(pluginOfGoal().goto(new goals.GoalNear(args.x, args.y, args.z, 1)), 20000)
}

async function followAction (args) {
  if (!pluginOfGoal()) throw new Error('fail')
  const player = bot.players[args.target]
  if (!player || !player.entity) throw new Error('fail')
  pluginOfGoal().setGoal(new goals.GoalFollow(player.entity, 2), true)
  await sleep(200)
}

async function lookAction (args) {
  const entity = entityNamed(args.target)
  if (!entity) throw new Error('fail')
  const height = entity.height || 1.6
  await bot.lookAt(entity.position.offset(0, height * 0.8, 0))
}

async function pulse (control, ticks) {
  const steps = Math.max(1, Math.min(40, Number(ticks) || 8))
  bot.setControlState(control, true)
  await sleep(steps * 50)
  bot.setControlState(control, false)
}

async function digAction (args) {
  let block = null
  if (typeof args.x === 'number') {
    block = bot.blockAt(new Vec3(args.x, args.y, args.z))
  } else if (args.block) {
    block = findBlockName(args.block, 64)
  }
  if (!block || block.name === 'air') throw new Error('fail')
  if (bot.tool && bot.tool.equipForBlock) {
    try { await bot.tool.equipForBlock(block, {}) } catch (err) { /* dig anyway */ }
  }
  await withTimeout(bot.dig(block), 20000)
}

async function placeAction (args) {
  const item = stackNamed(args.block)
  if (!item) throw new Error('fail')
  await bot.equip(item, 'hand')
  let x
  let y
  let z
  if (typeof args.x === 'number') {
    x = args.x
    y = args.y
    z = args.z
  } else {
    const pos = bot.entity.position
    x = Math.floor(pos.x) + 1
    y = Math.floor(pos.y)
    z = Math.floor(pos.z)
  }
  const below = bot.blockAt(new Vec3(Math.floor(x), Math.floor(y) - 1, Math.floor(z)))
  if (!below || below.name === 'air' || below.name === 'cave_air') throw new Error('fail')
  await withTimeout(bot.placeBlock(below, new Vec3(0, 1, 0)), 10000)
}

async function collectAction (args) {
  const aliases = { cobblestone: 'stone', raw_iron: 'iron_ore', sweet_berries: 'sweet_berry_bush' }
  const blockName = aliases[bare(args.item)] || bare(args.item)
  const block = findBlockName(blockName, 64)
  if (block && bot.collectBlock) {
    await withTimeout(bot.collectBlock.collect(block), 20000)
    return
  }
  const wanted = bare(args.item)
  const drop = Object.values(bot.entities).find((entity) => {
    if (!entity || entity.name !== 'item' || typeof entity.getDroppedItem !== 'function') return false
    const held = entity.getDroppedItem()
    return held && held.name === wanted
  })
  if (!drop || !bot.collectBlock) throw new Error('fail')
  await withTimeout(bot.collectBlock.collect(drop), 20000)
}

async function craftAction (args) {
  const data = mc()
  const info = data.itemsByName[bare(args.item)]
  if (!info) throw new Error('fail')
  const count = Math.max(1, Math.min(64, Number(args.count) || 1))
  const tableInfo = data.blocksByName.crafting_table
  const table = tableInfo ? bot.findBlock({ matching: tableInfo.id, maxDistance: 32 }) : null
  let recipes = bot.recipesFor(info.id, null, 1, table)
  if (!recipes || !recipes.length) recipes = bot.recipesFor(info.id, null, 1, null)
  if (!recipes || !recipes.length) throw new Error('fail')
  const recipe = recipes[0]
  await withTimeout(bot.craft(recipe, count, recipe.requiresTable ? table : null), 15000)
}

async function smeltAction (args) {
  const data = mc()
  const furnaceInfo = data.blocksByName.furnace
  if (!furnaceInfo) throw new Error('fail')
  const furnaceBlock = bot.findBlock({ matching: furnaceInfo.id, maxDistance: 32 })
  if (!furnaceBlock) throw new Error('fail')
  const input = data.itemsByName[bare(args.item)]
  const fuel = data.itemsByName[bare(args.fuel || 'coal')]
  if (!input || !fuel) throw new Error('fail')
  const count = Math.max(1, Math.min(64, Number(args.count) || 1))
  const furnace = await bot.openFurnace(furnaceBlock)
  try {
    await furnace.putFuel(fuel.id, null, 1)
    await furnace.putInput(input.id, null, count)
    const started = Date.now()
    while (Date.now() - started < 20000) {
      const output = typeof furnace.outputItem === 'function' ? furnace.outputItem() : null
      if (output) {
        await furnace.takeOutput()
        return
      }
      await sleep(400)
      if (stopped) return
    }
  } finally {
    try { furnace.close() } catch (err) { /* already shut */ }
  }
}

async function equipAction (args) {
  const item = stackNamed(args.item)
  if (!item) throw new Error('fail')
  const slot = SLOTS[args.slot] || 'hand'
  await bot.equip(item, slot)
  if (bot.armorManager && slot !== 'hand' && slot !== 'off-hand') {
    try { await bot.armorManager.equipAll() } catch (err) { /* the named piece is enough */ }
  }
}

async function unequipAction (args) {
  const slot = SLOTS[args.slot] || 'hand'
  await bot.unequip(slot)
}

async function useAction (args) {
  if (args.item) {
    const item = stackNamed(args.item)
    if (!item) throw new Error('fail')
    await bot.equip(item, 'hand')
  }
  bot.activateItem(false)
}

async function eatAction (args) {
  let name = bare(args.item || '')
  if (!name) {
    const found = bot.inventory.items().find((item) => FOOD.has(item.name))
    if (!found) throw new Error('fail')
    name = found.name
  }
  const item = stackNamed(name)
  if (!item) throw new Error('fail')
  await bot.equip(item, 'hand')
  await withTimeout(bot.consume(), 10000)
}

async function attackAction (args) {
  const entity = entityNamed(args.target)
  if (!entity) throw new Error('fail')
  if (bot.pvp) bot.pvp.attack(entity)
  else bot.attack(entity)
}

async function defendAction (args) {
  const entity = entityNamed(args.target)
  if (entity) await bot.lookAt(entity.position.offset(0, entity.height || 1, 0))
  const shield = stackNamed('shield')
  if (shield) {
    await bot.equip(shield, 'off-hand')
    bot.activateItem(true)
  }
  bot.setControlState('sneak', true)
}

function chestBlock (args) {
  if (typeof args.x === 'number') return bot.blockAt(new Vec3(args.x, args.y, args.z))
  const data = mc()
  const ids = ['chest', 'trapped_chest', 'barrel']
    .map((name) => data.blocksByName[name] && data.blocksByName[name].id)
    .filter((id) => id !== undefined)
  if (!ids.length) return null
  return bot.findBlock({ matching: ids, maxDistance: 32 })
}

async function openChestAction (args) {
  const block = chestBlock(args || {})
  if (!block) throw new Error('fail')
  closeChest()
  chest = await bot.openChest(block)
}

async function depositAction (args) {
  if (!chest) await openChestAction({})
  const info = mc().itemsByName[bare(args.item)]
  if (!info) throw new Error('fail')
  const count = Math.max(1, Math.min(64, Number(args.count) || 1))
  await chest.deposit(info.id, null, count)
}

async function withdrawAction (args) {
  if (!chest) await openChestAction({})
  const info = mc().itemsByName[bare(args.item)]
  if (!info) throw new Error('fail')
  const count = Math.max(1, Math.min(64, Number(args.count) || 1))
  await chest.withdraw(info.id, null, count)
}

async function discard (args, upward) {
  const item = stackNamed(args.item)
  if (!item) throw new Error('fail')
  const count = Math.max(1, Math.min(64, Number(args.count) || item.count))
  if (upward) await bot.look(bot.entity.yaw, -0.8, true)
  await bot.toss(item.type, null, count)
}

async function sleepAction () {
  const data = mc()
  const ids = Object.values(data.blocksByName)
    .filter((block) => block && typeof block.name === 'string' && block.name.endsWith('_bed'))
    .map((block) => block.id)
  if (!ids.length) throw new Error('fail')
  const bed = bot.findBlock({ matching: ids, maxDistance: 32 })
  if (!bed) throw new Error('fail')
  await withTimeout(bot.sleep(bed), 10000)
}

function chatAction (args) {
  const text = String(args.text || '')
  if (!text || text.length > 200 || /[\r\n]/.test(text)) throw new Error('fail')
  bot.chat(text)
}

const ACTIONS = {
  goto: gotoAction,
  follow: followAction,
  stop: async () => { haltMotion() },
  look_at: lookAction,
  jump: async () => { await pulse('jump', 5) },
  sprint: async (args) => { bot.setControlState('sprint', args.state === 'on') },
  sneak: async (args) => { bot.setControlState('sneak', args.state === 'on') },
  swim: async (args) => {
    const control = args.direction === 'up' ? 'jump' : args.direction === 'down' ? 'sneak' : 'forward'
    await pulse(control, args.ticks)
  },
  strafe: async (args) => { await pulse(args.direction, args.ticks) },
  dig: digAction,
  mine: digAction,
  place: placeAction,
  collect: collectAction,
  craft: craftAction,
  smelt: smeltAction,
  equip: equipAction,
  unequip: unequipAction,
  use: useAction,
  eat: eatAction,
  attack: attackAction,
  defend: defendAction,
  open_chest: openChestAction,
  deposit: depositAction,
  withdraw: withdrawAction,
  drop: async (args) => { await discard(args, false) },
  toss: async (args) => { await discard(args, true) },
  sleep: sleepAction,
  chat: async (args) => { chatAction(args) },
}

function round (value) {
  return Math.round(Number(value) * 100) / 100
}

function finite (value, low, high, fallback) {
  const number = Number(value)
  if (!Number.isFinite(number)) return fallback
  return Math.max(low, Math.min(high, number))
}

function observe () {
  if (!inWorld()) return { status: 'paused' }
  const pos = bot.entity.position
  const counts = new Map()
  for (const item of bot.inventory.items()) {
    counts.set(item.name, (counts.get(item.name) || 0) + item.count)
  }
  const inventory = []
  for (const [name, count] of counts) {
    if (inventory.length >= 12) break
    inventory.push({ item: name, count: Math.max(1, Math.min(999, count)) })
  }
  const origin = pos.floored()
  const found = []
  for (let dx = -3; dx <= 3; dx++) {
    for (let dy = -2; dy <= 2; dy++) {
      for (let dz = -3; dz <= 3; dz++) {
        const block = bot.blockAt(origin.offset(dx, dy, dz))
        if (!block || block.name === 'air' || block.name === 'cave_air' || block.name === 'void_air') continue
        found.push({
          name: block.name,
          x: block.position.x,
          y: block.position.y,
          z: block.position.z,
          d: dx * dx + dy * dy + dz * dz,
        })
      }
    }
  }
  found.sort((a, b) => a.d - b.d)
  const blocks = found.slice(0, 8).map((block) => ({
    name: block.name, x: block.x, y: block.y, z: block.z,
  }))
  const entities = []
  for (const entity of Object.values(bot.entities)) {
    if (!entity || entity === bot.entity || !entity.position) continue
    if (entity.name === 'item' || entity.type === 'object') continue
    const name = entity.username || entity.name
    if (!name) continue
    let kind = 'passive'
    if (entity.type === 'player' || entity.username) kind = 'player'
    else if (HOSTILE.has(name)) kind = 'hostile'
    const dx = entity.position.x - pos.x
    const dy = entity.position.y - pos.y
    const dz = entity.position.z - pos.z
    entities.push({
      name,
      x: round(entity.position.x),
      y: round(entity.position.y),
      z: round(entity.position.z),
      kind,
      d: dx * dx + dy * dy + dz * dz,
    })
  }
  entities.sort((a, b) => a.d - b.d)
  const trimmed = entities.slice(0, 6).map(({ d, ...rest }) => rest)
  const threats = trimmed.filter((entity) => entity.kind === 'hostile').map((entity) => entity.name)
  let clock = 0
  if (bot.time && Number.isFinite(bot.time.timeOfDay)) {
    clock = Math.floor(bot.time.timeOfDay) % 24001
  }
  const moving = Boolean(
    pluginOfGoal() && typeof pluginOfGoal().isMoving === 'function' && pluginOfGoal().isMoving()
  )
  const body = {
    status: moving || busy ? 'acting' : 'idle',
    position: { x: round(pos.x), y: round(pos.y), z: round(pos.z) },
    health: finite(bot.health, 0, 20, 0),
    food: finite(bot.food, 0, 20, 0),
    time_of_day: clock,
  }
  if (inventory.length) body.inventory = inventory
  if (blocks.length) body.blocks = blocks
  if (trimmed.length) body.entities = trimmed
  if (threats.length) body.threats = threats.slice(0, 8)
  if (bot.heldItem && bot.heldItem.name) body.held = bot.heldItem.name
  if (recentChat.length) body.chat = recentChat.slice(-3)
  return body
}

function flee () {
  haltMotion()
  const hostile = nearestHostile()
  if (!hostile || !pluginOfGoal()) {
    bot.setControlState('back', true)
    setTimeout(() => bot.setControlState('back', false), 400)
    return
  }
  const pos = bot.entity.position
  const dx = pos.x - hostile.position.x
  const dz = pos.z - hostile.position.z
  const length = Math.hypot(dx, dz) || 1
  pluginOfGoal().setGoal(new goals.GoalNear(
    pos.x + (dx / length) * 8,
    pos.y,
    pos.z + (dz / length) * 8,
    1,
  ))
}

function localReflex () {
  if (!inWorld()) return
  if (bot.health <= FLEE_AT) {
    flee()
    return
  }
  if (busy) return
  if (bot.food <= EAT_AT) {
    const food = bot.inventory.items().find((item) => FOOD.has(item.name))
    if (food) {
      busy = true
      eatAction({ item: food.name }).catch(() => {}).finally(() => { busy = false })
      return
    }
  }
  const hostile = nearestHostile()
  if (hostile) {
    const distance = bot.entity.position.distanceTo(hostile.position)
    if (distance <= ATTACK_WITHIN) {
      if (bot.pvp) bot.pvp.attack(hostile)
      else bot.attack(hostile)
      return
    }
    if (distance <= DODGE_WITHIN) {
      const side = dodge === 'left' ? 'right' : 'left'
      dodge = side
      bot.setControlState(side, true)
      setTimeout(() => bot.setControlState(side, false), 350)
      return
    }
  }
  const key = `${Math.round(bot.entity.position.x)}:${Math.round(bot.entity.position.z)}`
  const moving = Boolean(
    pluginOfGoal() && typeof pluginOfGoal().isMoving === 'function' && pluginOfGoal().isMoving()
  )
  if (moving && key === stuckKey) stuck += 1
  else {
    stuckKey = key
    stuck = 0
  }
  if (stuck === 4) bot.setControlState('jump', true)
  if (stuck === 5) bot.setControlState('jump', false)
  if (stuck >= 6) {
    bot.setControlState('jump', false)
    bot.setControlState('left', true)
    setTimeout(() => bot.setControlState('left', false), 300)
    stuck = 0
  }
}

async function act (args) {
  const name = args && args.action
  const handler = ACTIONS[name]
  if (!handler) {
    const error = UNKNOWN
    throw Object.assign(new Error(error), { fixed: error })
  }
  if (!inWorld()) {
    throw Object.assign(new Error(NO_WORLD), { fixed: NO_WORLD })
  }
  const mine = generation
  busy = true
  try {
    await handler(args.arguments || {})
    if (generation !== mine && name !== 'stop') {
      throw Object.assign(new Error(FAILED), { fixed: FAILED })
    }
  } finally {
    busy = false
  }
}

async function handle (message) {
  if (!message || typeof message !== 'object') {
    reply({ id: null, ok: false, error: 'The plan is not JSON.' })
    return
  }
  const id = message.id
  if (stopped) {
    reply({ id, ok: false, error: STOPPED })
    return
  }
  try {
    if (message.method === 'stop') {
      reply({ id, ok: true, result: { status: 'paused' } })
      shutdown()
      return
    }
    if (message.method === 'halt') {
      haltMotion()
      reply({ id, ok: true, result: { status: 'paused' } })
      return
    }
    if (message.method === 'observe') {
      reply({ id, ok: true, result: observe() })
      return
    }
    if (message.method === 'act') {
      await act(message)
      reply({ id, ok: true, result: { status: 'idle' } })
      return
    }
    reply({ id, ok: false, error: UNKNOWN })
  } catch (err) {
    reply({ id, ok: false, error: err && err.fixed ? err.fixed : FAILED })
  }
}

bot.once('spawn', () => {
  spawned = true
  try {
    const movements = new Movements(bot)
    if (pluginOfGoal()) pluginOfGoal().setMovements(movements)
  } catch (err) {
    console.error(FAILED)
  }
  if (bot.armorManager && bot.armorManager.equipAll) {
    bot.armorManager.equipAll().catch(() => {})
  }
})

bot.on('chat', (username, message) => {
  if (!username || username === bot.username) return
  const line = String(message).replace(/[\r\n]/g, ' ').trim().slice(0, 80)
  if (!line) return
  recentChat.push(line)
  if (recentChat.length > 6) recentChat.shift()
})

bot.on('error', () => { console.error(FAILED) })
bot.on('kicked', () => { console.error(FAILED) })
bot.on('end', () => { spawned = false })

setInterval(() => {
  if (stopped) return
  try { localReflex() } catch (err) { /* a reflex must not kill the process */ }
}, 400)

let chain = Promise.resolve()
const readline = require('readline')
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity })
lines.on('line', (line) => {
  let message
  try {
    message = JSON.parse(line)
  } catch (err) {
    reply({ id: null, ok: false, error: 'The plan is not JSON.' })
    return
  }
  chain = chain.then(() => handle(message)).catch(() => {})
})
lines.on('close', () => { shutdown() })

process.on('uncaughtException', () => { console.error(FAILED) })
process.on('unhandledRejection', () => { console.error(FAILED) })
