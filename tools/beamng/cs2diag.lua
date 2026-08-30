-- cs2diag: an in-game FPS isolation sweep for cs2beamng levels (Phase 10 §8.14).
--
-- Install: copy to <BeamNG userfolder>/lua/ge/extensions/cs2diag.lua
-- In-game console:  extensions.load('cs2diag')  then  cs2diag.start()
--
-- What it does: stands still wherever you are, measures a baseline, then hides one scene group
-- at a time (buildings/props, roads, markings, painted areas, vegetation, the far ring, the
-- terrain cut) for a few seconds each, unhides it, and measures again. The difference between
-- a phase's FPS and the baseline is what that group costs on this machine at this spot. Writes
-- cs2diag_report.json to the userfolder and prints a ranked summary to the console. Run it
-- twice: once parked downtown, once looking across the city from a hill.
--
-- cs2diag.stop() aborts and restores everything.

local M = {}

local PHASE_SECONDS = 8
local SETTLE_SECONDS = 2

local GROUPS = {
  { name = 'Objects', label = 'buildings and props' },
  { name = 'Networks', label = 'roads' },
  { name = 'Markings', label = 'road markings' },
  { name = 'Areas', label = 'painted areas' },
  { name = 'Vegetation', label = 'vegetation (forest)' },
  { name = 'FarRing', label = 'far skyline ring' },
  { name = 'TerrainCut', label = 'terrain cut' },
  { name = 'LaneFurniture', label = 'lane furniture' },
}

local state = nil

local function setHiddenRec(obj, value)
  if not obj then return end
  -- getObject(i) returns the child OBJECT (userdata), not an id; Sim.upcast gives it its real
  -- class so SceneObject.setHidden exists (the pattern BeamNG's own mainLevel.lua uses)
  if Sim and Sim.upcast then
    local up = Sim.upcast(obj)
    if up then obj = up end
  end
  if obj.setHidden then pcall(function() obj:setHidden(value) end) end
  if obj.getCount then
    for i = 0, obj:getCount() - 1 do
      setHiddenRec(obj:getObject(i), value)
    end
  end
end

local function phaseList()
  local phases = { { key = 'baseline', label = 'everything visible', group = nil } }
  for _, g in ipairs(GROUPS) do
    if scenetree.findObject(g.name) then
      table.insert(phases, { key = 'without ' .. g.name, label = g.label, group = g.name })
    end
  end
  table.insert(phases, { key = 'baseline2', label = 'everything visible again', group = nil })
  return phases
end

local function beginPhase(index)
  local phase = state.phases[index]
  if state.current and state.current.group then
    setHiddenRec(scenetree.findObject(state.current.group), false)
  end
  if not phase then
    M.finish()
    return
  end
  if phase.group then
    local ok, err = pcall(setHiddenRec, scenetree.findObject(phase.group), true)
    if not ok then
      log('E', 'cs2diag', 'hiding ' .. phase.group .. ' failed (' .. tostring(err) .. '); stopping and restoring')
      pcall(setHiddenRec, scenetree.findObject(phase.group), false)
      state = nil
      return
    end
  end
  state.current = phase
  state.index = index
  state.t = 0
  state.samples = {}
  log('I', 'cs2diag', string.format('phase %d/%d: %s', index, #state.phases, phase.key))
end

local function stats(samples)
  if #samples == 0 then return { fpsAvg = 0, fpsLow = 0, frames = 0 } end
  local sum = 0
  local sorted = {}
  for i, dt in ipairs(samples) do sum = sum + dt; sorted[i] = dt end
  table.sort(sorted)
  local worst = sorted[math.max(1, math.floor(#sorted * 0.95))] -- the slow tail
  return {
    fpsAvg = #samples / sum,
    fpsLow = 1.0 / worst,
    frames = #samples,
  }
end

local function onUpdate(dtReal)
  if not state or not state.current then return end
  state.t = state.t + dtReal
  if state.t > SETTLE_SECONDS then table.insert(state.samples, dtReal) end
  if state.t >= PHASE_SECONDS then
    state.results[state.current.key] = stats(state.samples)
    beginPhase(state.index + 1)
  end
end

function M.start(seconds)
  if state then log('W', 'cs2diag', 'already running; cs2diag.stop() first') return end
  if seconds then PHASE_SECONDS = seconds end
  state = { results = {}, phases = phaseList(), current = nil }
  state.mem0 = Engine.Render.getMemoryInfo()
  local pos = core_camera.getPosition()
  state.pos = pos and { pos.x, pos.y, pos.z } or nil
  log('I', 'cs2diag', string.format('sweep: %d phases x %ds - stand still', #state.phases, PHASE_SECONDS))
  beginPhase(1)
end

function M.stop()
  if state and state.current and state.current.group then
    setHiddenRec(scenetree.findObject(state.current.group), false)
  end
  state = nil
  log('I', 'cs2diag', 'stopped, everything restored')
end

function M.finish()
  local r = state.results
  local base = r.baseline or { fpsAvg = 0 }
  local rows = {}
  for key, s in pairs(r) do
    if key ~= 'baseline' and key ~= 'baseline2' then
      table.insert(rows, { key = key, gain = s.fpsAvg - base.fpsAvg, fpsAvg = s.fpsAvg, fpsLow = s.fpsLow })
    end
  end
  table.sort(rows, function(a, b) return a.gain > b.gain end)
  log('I', 'cs2diag', string.format('baseline %.1f fps (low %.1f); hiding a group changed it by:',
    base.fpsAvg, (r.baseline or {}).fpsLow or 0))
  for _, row in ipairs(rows) do
    log('I', 'cs2diag', string.format('  %+6.1f fps  %s (%.1f avg / %.1f low)', row.gain, row.key, row.fpsAvg, row.fpsLow))
  end
  local drift = (r.baseline2 or base).fpsAvg - base.fpsAvg
  log('I', 'cs2diag', string.format('  (baseline drift over the sweep: %+.1f fps - readings within that are noise)', drift))
  local report = {
    position = state.pos,
    phaseSeconds = PHASE_SECONDS,
    memoryBefore = state.mem0,
    memoryAfter = Engine.Render.getMemoryInfo(),
    results = r,
  }
  jsonWriteFile('cs2diag_report.json', report, true)
  log('I', 'cs2diag', 'written: cs2diag_report.json (userfolder root)')
  state = nil
end

M.onUpdate = onUpdate

return M
