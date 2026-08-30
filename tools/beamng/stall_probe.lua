-- Phase 10 §8.9 stall probe: is the Prefab:load() stall the .cdae PARSE or the OBJECT CREATION?
-- Run in the BeamNG console (GE Lua tab, `~`) after loading the cellprefab-converted level:
--   dofile("E:/csii-world-bundle/tools/beamng/stall_probe.lua")
-- Times, for the N largest never-loaded cells: first load (parse + create), unload, second
-- load (create only — shapes stay resident, §7), then one be:reloadCollision().
local N = 3
local function ms(f) local t = os.clock(); f(); return (os.clock() - t) * 1000 end
local cells = {}
for _, name in ipairs(scenetree.findClassObjects('Prefab')) do
  local p = scenetree.findObject(name)
  if p and not p:isLoaded() then
    local f = io.open((p.filename:gsub('^/', '')), 'rb')
    local size = 0
    if f then size = f:seek('end'); f:close() end
    if size == 0 then size = FS:stat(p.filename) and FS:stat(p.filename).filesize or 0 end
    table.insert(cells, {p = p, name = name, size = size})
  end
end
table.sort(cells, function(a, b) return a.size > b.size end)
log('I', 'stall_probe', string.format('%d unloaded prefabs; probing the %d largest', #cells, N))
local mem0 = Engine.Render.getMemoryInfo().usageBytes
for i = 1, math.min(N, #cells) do
  local c = cells[i]
  local t1 = ms(function() c.p:load() end)
  local m1 = Engine.Render.getMemoryInfo().usageBytes
  local tu = ms(function() c.p:unload() end)
  local t2 = ms(function() c.p:load() end)
  log('I', 'stall_probe', string.format('%s (%.1f MB json): first load %.0f ms | unload %.0f ms | second load %.0f ms | +%.0f MB',
    c.name, c.size / 1e6, t1, tu, t2, (m1 - mem0) / 1e6))
end
local tc = ms(function() be:reloadCollision() end)
log('I', 'stall_probe', string.format('reloadCollision %.0f ms', tc))
