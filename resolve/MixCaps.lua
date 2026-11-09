--[[
  MixCaps bridge for DaVinci Resolve (free + Studio).
  Workspace > Scripts > MixCaps

  Free Resolve blocks script windows (UIManager is Studio-only), so this script has no window:
  it opens the MixCaps app (a local page in your browser) and then quietly does the Resolve work
  the app asks for - render timeline audio, move the playhead, place Text+ captions.
  The two talk through files in %LOCALAPPDATA%\MixCaps\ipc. Nothing goes online.
]]

local VERSION = "0.3.0"

resolve = resolve or Resolve()
local SEP = "\\"
local APP = (os.getenv("LOCALAPPDATA") or ".") .. SEP .. "MixCaps"
local IPC = APP .. SEP .. "ipc"

-------------------------------------------------------------------------- small helpers
local function exists(p) local f = io.open(p, "rb"); if f then f:close(); return true end; return false end
local function readall(p) local f = io.open(p, "rb"); if not f then return nil end; local s = f:read("*a"); f:close(); return s end
local function writeall(p, s)
  local tmp = p .. ".tmp"
  local f = io.open(tmp, "wb"); if not f then return false end
  f:write(s); f:close()
  os.remove(p)
  return os.rename(tmp, p)
end

-- small files the app polls: write in place (a rename fails while the app has the file open)
local function writedirect(p, s)
  local f = io.open(p, "wb"); if not f then return false end
  f:write(s); f:close(); return true
end

local logpath = APP .. SEP .. "resolve_log.txt"
local function log(...)
  local parts = {}
  for i = 1, select("#", ...) do parts[#parts + 1] = tostring(select(i, ...)) end
  local f = io.open(logpath, "ab")
  if f then f:write(os.date("%Y-%m-%d %H:%M:%S ") .. table.concat(parts, " ") .. "\r\n"); f:close() end
  print("[MixCaps] " .. table.concat(parts, " "))
end

local sleep
do
  if bmd and type(bmd.wait) == "function" then
    sleep = function(s) bmd.wait(s) end
  else
    local ok, ffi = pcall(require, "ffi")
    if ok then
      pcall(ffi.cdef, "void Sleep(unsigned long ms);")
      sleep = function(s) ffi.C.Sleep(math.floor(s * 1000)) end
    else
      sleep = function(s) local t = os.clock() + s; while os.clock() < t do end end
    end
  end
end

-- minimal JSON encoder (results go to the Python app)
local function json(v)
  local t = type(v)
  if t == "nil" then return "null"
  elseif t == "boolean" then return v and "true" or "false"
  elseif t == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    return string.format("%.10g", v)
  elseif t == "string" then
    return '"' .. v:gsub('[%c"\\]', function(c)
      local m = { ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t" }
      return m[c] or string.format("\\u%04x", c:byte())
    end) .. '"'
  elseif t == "table" then
    if #v > 0 or next(v) == nil then
      local out = {}
      for i = 1, #v do out[i] = json(v[i]) end
      return "[" .. table.concat(out, ",") .. "]"
    end
    local out = {}
    for k, x in pairs(v) do out[#out + 1] = json(tostring(k)) .. ":" .. json(x) end
    return "{" .. table.concat(out, ",") .. "}"
  end
  return json(tostring(v))
end

local function frames_to_tc(frame, fps)
  local r = math.floor(fps + 0.5)
  local f = math.floor(frame + 0.5)
  local h = math.floor(f / (3600 * r)); f = f - h * 3600 * r
  local m = math.floor(f / (60 * r)); f = f - m * 60 * r
  local s = math.floor(f / r); f = f - s * r
  return string.format("%02d:%02d:%02d:%02d", h, m, s, f)
end

-------------------------------------------------------------------------- config / single instance
local okcfg, cfg = pcall(dofile, APP .. SEP .. "config.lua")
if not okcfg or type(cfg) ~= "table" then
  log("MixCaps is not installed: run install.bat first. (" .. tostring(cfg) .. ")")
  return
end
os.execute('mkdir "' .. IPC .. '" >nul 2>nul')

local function open_app()
  os.execute('start "" "' .. cfg.pythonw .. '" "' .. cfg.app_py .. '"')
end

local HB = IPC .. SEP .. "bridge.txt"
math.randomseed(os.time())
local token = tostring(os.time()) .. "-" .. tostring(math.random(100000, 999999))
do
  local s = readall(HB)
  local t = s and tonumber(s:match("^(%d+)"))
  if t and os.time() - t <= 3 then
    log("bridge already running; opening the app")
    open_app()
    return
  end
end

-------------------------------------------------------------------------- Resolve helpers
-- Re-acquire Resolve on every use: a long-running script can outlive the Resolve it started in
-- (e.g. after Resolve is restarted), and then its old handles silently return nil.
local ORIG = resolve
local function usable(r)
  if not r then return nil end
  local ok, pm = pcall(function() return r:GetProjectManager() end)
  -- a dead handle can still hand back an object whose methods are missing
  if ok and pm and pcall(function() return pm:GetCurrentProject() end) then return pm end
  return nil
end

local function live_resolve()
  local cands = {}
  if bmd and bmd.scriptapp then
    local ok, r = pcall(bmd.scriptapp, "Resolve")
    if ok and r then cands[#cands + 1] = r end
  end
  if type(Resolve) == "function" then
    local ok, r = pcall(Resolve)
    if ok and r then cands[#cands + 1] = r end
  end
  cands[#cands + 1] = ORIG
  for _, r in ipairs(cands) do
    local pm = usable(r)
    if pm then resolve = r; return r, pm end
  end
  return nil
end

local function cur()
  local pm = usable(resolve)
  local r = resolve
  if not pm then r, pm = live_resolve() end
  if not r then return nil, nil end
  local project = pm:GetCurrentProject()
  local timeline = project and project:GetCurrentTimeline()
  return project, timeline
end

local function tl_info()
  local project, tl = cur()
  if not tl then return { ok = false, error = "Open a timeline in Resolve first." } end
  local fpsS = tl:GetSetting("timelineFrameRate") or project:GetSetting("timelineFrameRate")
  local fps = tonumber(tostring(fpsS):match("[%d%.]+")) or 24
  local w = tonumber(tl:GetSetting("timelineResolutionWidth") or project:GetSetting("timelineResolutionWidth")) or 1920
  local h = tonumber(tl:GetSetting("timelineResolutionHeight") or project:GetSetting("timelineResolutionHeight")) or 1080
  local marks = tl:GetMarkInOut() or {}
  local mk = marks.audio or marks.video or {}
  return {
    ok = true, project = project:GetName(), timeline = tl:GetName(), fps = fps,
    start = tl:GetStartFrame(), finish = tl:GetEndFrame(), width = w, height = h,
    markIn = mk["in"], markOut = mk["out"], videoTracks = tl:GetTrackCount("video"),
    bridge = VERSION,
  }
end

-------------------------------------------------------------------------- tasks (state machines stepped by the loop)
local active = nil

local function set_status(id, pct, msg)
  writedirect(IPC .. SEP .. "status.json", json({ id = id, pct = pct, msg = msg }))
end

local function reply(id, res)
  res.id = id
  writeall(IPC .. SEP .. "result_" .. tostring(id) .. ".json", json(res))
end

local RESTORE = "MixCaps_restore_tmp"

local function job_render(cmd)
  local project, tl = cur()
  if not tl then return nil, "Open a timeline in Resolve first." end
  local info = tl_info()
  local prev = project:GetCurrentRenderFormatAndCodec() or {}
  if project:IsRenderingInProgress() then return nil, "Resolve is already rendering something. Wait for it to finish, then try again." end
  -- Resolve won't start a scripted render job that was created while the Deliver page is showing,
  -- so build the job from another page and go back afterwards.
  local backPage = resolve:GetCurrentPage()
  local hopped = false
  local function hop(page)
    pcall(function() resolve:OpenPage(page) end)
    hopped = true
    sleep(1.0)
  end
  if backPage == "deliver" then hop("edit") end
  local function back() if hopped and backPage then pcall(function() resolve:OpenPage(backPage) end) end end
  local function restore()
    if prev.format and prev.codec then pcall(function() project:SetCurrentRenderFormatAndCodec(prev.format, prev.codec) end) end
    pcall(function() project:SetRenderSettings({ ExportVideo = true, ExportAudio = true, SelectAllFrames = true }) end)
  end
  -- The API takes short format names ("wav", "mov"). Free Resolve lists no WAV codecs through the API,
  -- so fall back to an audio-only MOV with uncompressed PCM (the engine reads either).
  local wavCodec
  for _, name in pairs(project:GetRenderCodecs("wav") or {}) do wavCodec = wavCodec or name end
  local fmt, codec, ext
  local offset = 0
  local function setup()
    if wavCodec and project:SetCurrentRenderFormatAndCodec("wav", wavCodec) then
      fmt, codec, ext = "wav", wavCodec, "wav"
    elseif project:SetCurrentRenderFormatAndCodec("mov", "H264") then
      fmt, codec, ext = "mov", "H264", "mov"
    elseif project:SetCurrentRenderFormatAndCodec("mp4", "H264") then
      fmt, codec, ext = "mp4", "H264", "mp4"
    else
      return nil, "Resolve wouldn't accept any audio render format (wav / mov / mp4)."
    end
    local settings = { TargetDir = cmd.dir, CustomName = "mixcaps_audio", ExportVideo = false, ExportAudio = true,
                       AudioSampleRate = 48000, AudioBitDepth = 16 }
    if fmt ~= "wav" then settings.AudioCodec = "lpcm" end
    if cmd.marks and info.markIn and info.markOut then
      local a, b = info.markIn, info.markOut
      if a < info.start then a = a + info.start; b = b + info.start end
      settings.SelectAllFrames = false
      settings.MarkIn, settings.MarkOut = a, b
      offset = (a - info.start) / info.fps
    else
      settings.SelectAllFrames = true
    end
    if not project:SetRenderSettings(settings) then
      settings.AudioCodec = nil
      if not project:SetRenderSettings(settings) then return nil, "Resolve refused the audio render settings." end
    end
    local id = project:AddRenderJob()
    if not id then return nil, "Could not add the audio render job." end
    return id
  end
  -- Only ever start OUR job (never StartRendering() with no ids: that would start the user's whole queue).
  local jid, err, started
  for attempt = 1, 3 do
    jid, err = setup()
    if not jid then restore(); back(); return nil, err end
    started = project:StartRendering(jid)
    log("StartRendering attempt", attempt, "page", tostring(resolve:GetCurrentPage()), tostring(started))
    if started then break end
    pcall(function() project:DeleteRenderJob(jid) end)
    hop(attempt == 1 and "cut" or "edit")
  end
  if not started then
    restore(); back()
    return nil, "Resolve refused to start the audio render. Save your project, restart Resolve, then run Workspace > Scripts > MixCaps again."
  end
  log("rendering audio", fmt, codec, "->", cmd.dir)
  return function()
    if project:IsRenderingInProgress() then
      local st = project:GetRenderJobStatus(jid) or {}
      set_status(cmd.id, st.CompletionPercentage or 0, "Rendering timeline audio")
      return false
    end
    local st = project:GetRenderJobStatus(jid) or {}
    pcall(function() project:DeleteRenderJob(jid) end)
    restore()
    back()
    local path = cmd.dir .. SEP .. "mixcaps_audio." .. ext
    if not exists(path) then
      for _, e in ipairs({ "wav", "mov", "mp4" }) do
        if exists(cmd.dir .. SEP .. "mixcaps_audio." .. e) then path = cmd.dir .. SEP .. "mixcaps_audio." .. e end
      end
    end
    log("render finished", tostring(st.JobStatus), tostring(st.Error or ""), path, tostring(exists(path)))
    if not exists(path) then
      return true, { ok = false, error = "Audio render failed in Resolve (" .. tostring(st.JobStatus) .. " " .. tostring(st.Error or "") .. "). Check the Deliver page's render queue for the reason." }
    end
    return true, { ok = true, audio = path, offset = offset, info = info }
  end
end

local function find_bin(mp)
  local root = mp:GetRootFolder()
  for _, f in ipairs(root:GetSubFolderList() or {}) do
    if f:GetName() == "MixCaps" then return f end
  end
  return mp:AddSubFolder(root, "MixCaps")
end

-------------------------------------------------------------------------- Text+ template
-- Captions are normal Text+ titles (editable in the Edit page Inspector, follow any timeline size).
-- A script can't create a title in the Media Pool, so MixCaps keeps one "MixCaps Caption" Text+ template:
--   * if the MixCaps bin already has one, use it;
--   * else import it from assets\mixcaps-template.drb (shipped with MixCaps);
--   * else, if someone dragged a plain Text+ into the MixCaps bin, adopt it and save it as that .drb.
local TEMPLATE_NAME = "MixCaps Caption"
local TEMPLATE_DRB = cfg.assets .. SEP .. "mixcaps-template.drb"

local function is_title(c)
  local ok, t = pcall(function() return c:GetClipProperty("Type") end)
  return ok and t and tostring(t):find("Title") ~= nil
end

local function walk_titles(folder, list)
  for _, c in ipairs(folder:GetClipList() or {}) do
    if is_title(c) then list[#list + 1] = { clip = c, folder = folder } end
  end
  for _, sub in ipairs(folder:GetSubFolderList() or {}) do walk_titles(sub, list) end
  return list
end

local function find_template(mp)
  local bin = find_bin(mp)
  local titles = walk_titles(bin, {})
  for _, t in ipairs(titles) do
    if t.clip:GetName() == TEMPLATE_NAME then return t.clip end
  end
  -- shipped template
  if exists(TEMPLATE_DRB) then
    local prev = mp:GetCurrentFolder()
    mp:SetCurrentFolder(bin)
    local ok = mp:ImportFolderFromFile(TEMPLATE_DRB)
    if prev then mp:SetCurrentFolder(prev) end
    log("imported template drb", tostring(ok))
    for _, t in ipairs(walk_titles(bin, {})) do
      if t.clip:GetName() == TEMPLATE_NAME then return t.clip end
    end
  end
  -- adopt a Text+ the user dragged into the MixCaps bin, and save it for every other project / PC
  if #titles > 0 then
    local clip = titles[1].clip
    local sub
    for _, f in ipairs(bin:GetSubFolderList() or {}) do if f:GetName() == "MixCaps Template" then sub = f end end
    sub = sub or mp:AddSubFolder(bin, "MixCaps Template")
    pcall(function() mp:MoveClips({ clip }, sub) end)
    pcall(function() clip:SetName(TEMPLATE_NAME) end)
    if clip:GetName() ~= TEMPLATE_NAME then pcall(function() clip:SetClipProperty("Clip Name", TEMPLATE_NAME) end) end
    local okx = pcall(function() return sub:Export(TEMPLATE_DRB) end)
    log("adopted dragged Text+ as template; saved drb:", tostring(okx), TEMPLATE_DRB)
    return clip
  end
  return nil
end

-- Captions adapt to any frame size: Text+ "Size" scales with the frame WIDTH (measured), so size from the
-- frame's SHORT side. Vertical frames get bigger, Shorts-style text.
local function geometry(W, H, scale, posY)
  local short = math.min(W, H)
  local aspect = W / H
  local r
  if aspect >= 1.2 then r = 0.1067        -- landscape (16:9 -> size 0.06 at 1920x1080)
  elseif aspect <= 0.85 then r = 0.12      -- vertical Shorts / Reels (9:16)
  else r = 0.11 end                        -- square-ish
  r = r * (scale or 1)
  return { size = r * short / W, dx = 0.06, posY = posY or (aspect <= 0.85 and 0.33 or 0.15), rel = r }
end

local function textplus_of(item)
  local comp = item:GetFusionCompByIndex(1)
  if not comp then return nil end
  local t = comp:FindTool("Template")
  if not t then
    local list = comp:GetToolList(false, "TextPlus") or {}
    local _, first = next(list)
    t = first
  end
  return comp, t
end

--@@LOOK_BEGIN
-- A "look" (preset) is plain data from the app, all relative so it fits any frame size:
--   font, fontStyle, color {r,g,b}, tracking
--   outline = { color, width }                       Text+ element 2 (text outline)
--   shadow  = { color, x, y, blur, opacity }          Text+ element 3 (offset copy of the text)
--   box     = { color, opacity, padX, padY, round }   Text+ element 4 (filled box behind each line)
--   anim    = "pop" | "fade" | "slide" | "type" | "none"
local function setrgb(t, n, c, a)
  c = c or { 1, 1, 1 }
  t:SetInput("Red" .. n, c[1]); t:SetInput("Green" .. n, c[2]); t:SetInput("Blue" .. n, c[3])
  t:SetInput("Alpha" .. n, a or 1)
end

local function unanimate(t, id)
  pcall(function() t[id]:ConnectTo(nil) end)
end

local function keys(t, id, list, kind)
  t:AddModifier(id, kind or "BezierSpline")
  for _, k in ipairs(list) do t[id][k[1]] = k[2] end
end

-- size: Text+ Size for this frame; posY: 0..1 from bottom; fps: timeline fps
local function apply_look(comp, t, look, size, posY, fps)
  look = look or {}
  fps = fps or 30
  local a = comp:GetAttrs()
  local t0 = a.COMPN_GlobalStart or 0
  local t1 = a.COMPN_GlobalEnd or (t0 + fps)
  local f = function(sec) return math.max(1, math.floor(sec * fps + 0.5)) end
  -- Fusion keeps the value at the comp's CURRENT frame when a spline is added or removed,
  -- so work at the first frame: old animation is cleared cleanly and new keys start from scratch
  local prevTime = comp.CurrentTime
  pcall(function() comp.CurrentTime = t0 end)
  for _, id in ipairs({ "Size", "Center", "End", "Opacity1", "Opacity2", "Opacity3", "Opacity4", "SoftnessX1", "SoftnessY1" }) do unanimate(t, id) end
  t:SetInput("SoftnessX1", 0); t:SetInput("SoftnessY1", 0)

  t:SetInput("Font", look.font or "Poppins")
  t:SetInput("Style", look.fontStyle or "Bold")
  t:SetInput("CharacterSpacing", look.tracking or 1)
  t:SetInput("Enabled1", 1)
  t:SetInput("Opacity1", 1)
  setrgb(t, 1, look.color)

  local o = look.outline
  if o and (o.width or 0) > 0 then
    t:SetInput("Enabled2", 1)
    t:SetInput("ElementShape2", 1)          -- text outline
    t:SetInput("Thickness2", o.width)
    t:SetInput("OutsideOnly2", 1)
    t:SetInput("JoinStyle2", 1)             -- round joins (no spikes on M, W)
    t:SetInput("Level2", 3)
    t:SetInput("Opacity2", 1)
    setrgb(t, 2, o.color or { 0, 0, 0 })
  else
    t:SetInput("Enabled2", 0)
  end

  local s = look.shadow
  if s then
    t:SetInput("Enabled3", 1)
    t:SetInput("ElementShape3", 0)
    t:SetInput("Opacity3", s.opacity or 1)
    setrgb(t, 3, s.color or { 0, 0, 0 })
    t:SetInput("SoftnessX3", s.blur or 0); t:SetInput("SoftnessY3", s.blur or 0)
    t:SetInput("SoftnessGlow3", 0)          -- Text+ glow breaks into colour rings; never use it
    t:SetInput("Offset3", { s.x or 0, s.y or 0 })
  else
    t:SetInput("Enabled3", 0)
  end

  local b = look.box
  if b then
    t:SetInput("Enabled4", 1)
    t:SetInput("ElementShape4", 2)          -- border fill = solid box
    t:SetInput("Level4", 1)                 -- one box per line
    t:SetInput("ExtendHorizontal4", b.padX or 0.3)
    t:SetInput("ExtendVertical4", b.padY or 0.15)
    t:SetInput("Round4", b.round or 0.3)
    t:SetInput("Opacity4", b.opacity or 1)
    setrgb(t, 4, b.color or { 0, 0, 0 })
  else
    t:SetInput("Enabled4", 0)
  end

  -- animation: key the entrance (old keys were cleared above)
  t:SetInput("Size", size)
  t:SetInput("Center", { 0.5, posY })
  t:SetInput("End", 1)
  local anim = look.anim or "pop"
  local len = t1 - t0
  if anim == "pop" then
    keys(t, "Size", { { t0, size * 0.72 }, { t0 + f(0.1), size * 1.08 }, { t0 + f(0.17), size } })
  elseif anim == "fade" or anim == "slide" or anim == "blur" then
    if anim == "blur" then
      -- out-of-focus to sharp: soften the text fill (and its colour) and bring it into focus
      local d = math.min(f(0.3), math.max(1, math.floor(len / 3)))
      t:SetInput("SoftnessOnFillColorToo1", 1)
      keys(t, "SoftnessX1", { { t0, look.blurIn or 6 }, { t0 + d, 0 } })
      keys(t, "SoftnessY1", { { t0, look.blurIn or 6 }, { t0 + d, 0 } })
    end
    local d = math.min(f(0.2), math.max(1, math.floor(len / 3)))
    -- only enabled elements have Opacity inputs
    local on = { true, o and (o.width or 0) > 0, s ~= nil, b ~= nil }
    local ops = { 1, 1, (s and s.opacity) or 1, (b and b.opacity) or 1 }
    for n = 1, 4 do
      if on[n] then pcall(keys, t, "Opacity" .. n, { { t0, 0 }, { t0 + d, ops[n] } }) end
    end
    if anim == "slide" then
      keys(t, "Center", { { t0, { 0.5, posY - 0.04 } }, { t0 + d, { 0.5, posY } } }, "XYPath")
    end
  elseif anim == "type" then
    local d = math.min(f(0.45), math.max(1, math.floor(len * 0.6)))
    keys(t, "End", { { t0, 0 }, { t0 + d, 1 } })
  end
  pcall(function() comp.CurrentTime = prevTime end)
end
--@@LOOK_END

local function style_title(item, text, st)
  local comp, t = textplus_of(item)
  if not t then return false end
  comp:Lock()
  t:SetInput("StyledText", st.upper and string.upper(text) or text)
  apply_look(comp, t, st.look, st.size, st.posY, st.fps)
  -- remember the look + original text, so "Fit" / "Apply look" can redo it for any size or style
  pcall(function() t:SetData("MixCaps", { rel = st.rel, scale = st.scale, posY = st.posY, auto = st.autoPos,
                                           look = st.look, text = text, upper = st.upper, stacked = st.stacked }) end)
  comp:Unlock()
  return true
end

local function job_insert(cmd)
  local project, tl = cur()
  if not tl then return nil, "Open a timeline in Resolve first." end
  local info = tl_info()
  local mp = project:GetMediaPool()
  local template = find_template(mp)
  if not template then
    return nil, "One-time setup: in Resolve open Effects > Titles, drag \"Text+\" into the \"MixCaps\" bin in the Media Pool, then press Add again. (MixCaps saves it as its template, so this never comes up again.)"
  end
  local tplLen = tonumber(template:GetClipProperty("Frames")) or 0
  -- a title's length is counted in the TEMPLATE's own frame rate (ours is 30 fps), not the timeline's
  local tplFps = tonumber(tostring(template:GetClipProperty("FPS")):match("[%d%.]+")) or info.fps
  local caps = cmd.captions or {}
  local offset = cmd.offset or 0
  -- stacked presets put line N of a block on track "MixCaps N" (slot N-1); plain presets use slot 0 only
  local nslots = 1
  for _, c in ipairs(caps) do nslots = math.max(nslots, (c.slot or 0) + 1) end
  -- reuse the MixCaps tracks: pressing Add again REPLACES all previous MixCaps captions
  local slotTrack, madeTracks = {}, {}
  for t = 1, tl:GetTrackCount("video") do
    local nm = tl:GetTrackName("video", t) or ""
    local n = (nm == "MixCaps") and 1 or tonumber(nm:match("^MixCaps (%d+)$"))
    if n then
      local old = tl:GetItemListInTrack("video", t) or {}
      if #old > 0 then log("replacing", #old, "previous MixCaps captions on V" .. t); tl:DeleteClips(old, false) end
      slotTrack[n] = slotTrack[n] or t
    end
  end
  for n = 1, nslots do
    if not slotTrack[n] then
      if not tl:AddTrack("video") then return nil, "Could not add a video track." end
      local t = tl:GetTrackCount("video")
      slotTrack[n] = t
      madeTracks[#madeTracks + 1] = t
      pcall(function() tl:SetTrackName("video", t, n == 1 and "MixCaps" or ("MixCaps " .. n)) end)
    end
    pcall(function() if tl:GetIsTrackLocked("video", slotTrack[n]) then tl:SetTrackLock("video", slotTrack[n], false) end end)
  end
  local track = slotTrack[1]

  local st = cmd.style or {}
  local look = st.look
  if not look then
    -- older app: GameGlitch with on/off switches
    look = { font = "Poppins", fontStyle = "Bold", color = { 1, 1, 1 },
             shadow = st.shadow ~= false and { color = { 0.659, 0.333, 0.969 }, x = 0.06, y = 0 } or nil,
             anim = st.pop == false and "none" or "pop" }
  end
  local W, H = info.width, info.height
  local stacked = st.looks ~= nil
  -- stacked blocks grow DOWN from this height (0..1 from the bottom), plain captions sit at it
  local autoY = stacked and ((W / H) <= 0.85 and 0.6 or 0.5) or nil
  local g = geometry(W, H, st.scale or look.scale, st.posY or autoY)
  st.look, st.size, st.posY, st.rel, st.fps, st.autoPos = look, g.size, g.posY, g.rel, info.fps, st.posY == nil
  st.upper = cmd.upper and true or false

  -- per-caption style: plain = the one look; stacked = its line look, size and stacked position
  local cst = {}
  local blockId, y, prevH
  for i, c in ipairs(caps) do
    if stacked and c.li and st.looks[c.li] then
      local lk = st.looks[c.li]
      local gg = geometry(W, H, (st.scale or 1) * (lk.scale or 1), nil)
      local h = gg.rel * math.min(W, H) / H * 0.68         -- one line's cap height as a fraction of the frame
      if c.block ~= blockId then blockId, y = c.block, g.posY
      else y = y - (prevH + h) / 2 - 0.006 end
      prevH = h
      cst[i] = { look = lk, size = gg.size, rel = gg.rel, scale = (st.scale or 1) * (lk.scale or 1), posY = y,
                 autoPos = false, fps = info.fps, upper = st.upper, stacked = true }
    else
      cst[i] = st
    end
  end

  -- whole frames, inside the timeline, never overlapping on a track (else Resolve silently rejects the batch)
  local infos, capIndex = {}, {}
  local tlEnd = info.finish or math.huge
  local prevEnd = {}
  for i, c in ipairs(caps) do
    local slot = (c.slot or 0) + 1
    local rf = info.start + math.floor((c.s + offset) * info.fps + 0.5)
    local ef = info.start + math.floor(((c.be or c.e) + offset) * info.fps + 0.5)  -- stacked: hold until the block clears
    if rf < (prevEnd[slot] or info.start) then rf = prevEnd[slot] or info.start end
    if ef > tlEnd then ef = tlEnd end
    if ef - rf >= 1 then
      -- round DOWN: rounding up can overlap the next caption by a frame, which shifts it
      local srcLen = math.max(1, math.floor((ef - rf) / info.fps * tplFps))
      infos[#infos + 1] = { mediaPoolItem = template, startFrame = 0, endFrame = srcLen,
                            trackIndex = slotTrack[slot], recordFrame = rf, mediaType = 1 }
      capIndex[#infos] = i
      prevEnd[slot] = ef
    end
  end
  set_status(cmd.id, 0, "Placing " .. #infos .. " Text+ captions")
  local items = mp:AppendToTimeline(infos) or {}
  log("template", template:GetName(), "frames", tplLen, "; appended", #items, "of", #infos, "on", nslots, "track(s) from V" .. track)
  local byStart = {}
  local key = function(tr, f) return tr .. ":" .. f end
  for n = 1, nslots do
    for _, it in ipairs(tl:GetItemListInTrack("video", slotTrack[n]) or {}) do
      local ok, s0 = pcall(function() return it:GetStart() end)
      if ok and s0 then byStart[key(slotTrack[n], s0)] = it end
    end
  end
  local placed = 0
  for _ in pairs(byStart) do placed = placed + 1 end
  if placed < #infos then
    for _, inf in ipairs(infos) do
      if not byStart[key(inf.trackIndex, inf.recordFrame)] then
        local r = mp:AppendToTimeline({ inf }) or {}
        local ok, s0 = pcall(function() return r[1] and r[1]:GetStart() end)
        if ok and s0 then byStart[key(inf.trackIndex, s0)] = r[1]; placed = placed + 1 end
      end
    end
  end
  log("placed", placed, "of", #infos)
  if placed == 0 then
    for k = #madeTracks, 1, -1 do pcall(function() tl:DeleteTrack("video", madeTracks[k]) end) end
    return nil, "Resolve did not place the Text+ captions (see resolve_log.txt)."
  end
  local first = byStart[key(infos[1].trackIndex, infos[1].recordFrame)]
  if first then
    log("first caption length", first:GetDuration(), "timeline frames (" .. infos[1].endFrame .. " template frames at",
        tplFps, "fps; timeline", info.fps, "fps)")
  end
  local i, done, failed = 0, 0, 0
  return function()
    for _ = 1, 10 do
      i = i + 1
      if i > #infos then
        return true, { ok = true, placed = done, failed = failed, track = track, tracks = nslots, appended = #items }
      end
      local inf = infos[i]
      local it = byStart[key(inf.trackIndex, inf.recordFrame)]
      if not it then
        -- tolerate a frame or two of drift
        for d = 1, 3 do
          it = byStart[key(inf.trackIndex, inf.recordFrame + d)] or byStart[key(inf.trackIndex, inf.recordFrame - d)]
          if it then break end
        end
      end
      if it then
        local ok, res = pcall(style_title, it, caps[capIndex[i]].t, cst[capIndex[i]])
        if ok and res then done = done + 1 else failed = failed + 1; log("style failed", i, tostring(res)) end
      else
        failed = failed + 1
        log("no placed clip found for caption", i, "at", infos[i].recordFrame)
      end
    end
    set_status(cmd.id, math.floor(100 * i / #infos), "Styling captions " .. i .. " / " .. #infos)
    return false
  end
end

-- Re-fit every MixCaps caption to the timeline's current shape (Text+ titles already follow the
-- resolution; this redoes size + position when the aspect changes, e.g. 16:9 -> 9:16).
local function job_refit(cmd)
  local _, tl = cur()
  if not tl then return nil, "Open a timeline in Resolve first." end
  local info = tl_info()
  local W, H = info.width, info.height
  local items = {}
  for t = 1, tl:GetTrackCount("video") do
    local nm = tl:GetTrackName("video", t) or ""
    if nm == "MixCaps" or nm:match("^MixCaps %d+$") then
      for _, it in ipairs(tl:GetItemListInTrack("video", t) or {}) do items[#items + 1] = it end
    end
  end
  if #items == 0 then return nil, "No MixCaps captions on this timeline." end
  if cmd.style then
    -- stacked captions are laid out per block; a new look needs a fresh layout
    for _, it in ipairs(items) do
      local ok, d = pcall(function() local _, t = textplus_of(it); return t and t:GetData("MixCaps") end)
      if ok and type(d) == "table" and d.stacked then
        return nil, "These captions are stacked, so a new look needs a fresh layout. Press \"Add captions to timeline\" instead (it replaces them)."
      end
      break
    end
  end
  local i, done = 0, 0
  return function()
    for _ = 1, 15 do
      i = i + 1
      if i > #items then return true, { ok = true, refit = done, total = #items, width = W, height = H } end
      pcall(function()
        local comp, t = textplus_of(items[i])
        if not t then return end
        local d = t:GetData("MixCaps") or {}
        local ns = cmd.style  -- "Apply look": a new look (+ size, position, case) for captions already placed
        if ns then
          d.look = ns.look or d.look
          d.scale = ns.scale or d.scale
          d.rel = geometry(W, H, d.scale or 1, nil).rel
          d.auto = ns.posY == nil
          d.posY = ns.posY
          d.upper = cmd.upper and true or false
          if d.text then t:SetInput("StyledText", d.upper and string.upper(d.text) or d.text) end
        end
        local base = geometry(W, H, 1, nil)
        local rel = d.rel or base.rel
        local size = rel * math.min(W, H) / W
        -- auto position follows the new frame shape; a position the user picked (or a stacked line) is kept
        local posY = ((d.auto == false or d.stacked) and d.posY) or base.posY
        local look = d.look or { anim = d.pop and "pop" or "none",
                                 shadow = { color = { 0.659, 0.333, 0.969 }, x = 0.06, y = 0 } }
        comp:Lock()
        apply_look(comp, t, look, size, posY, info.fps)
        d.posY = posY
        pcall(function() t:SetData("MixCaps", d) end)
        comp:Unlock()
        done = done + 1
      end)
    end
    set_status(cmd.id, math.floor(100 * i / #items), (cmd.style and "Styling captions " or "Fitting captions ") .. i .. " / " .. #items)
    return false
  end
end

-- Where every audio clip on the timeline comes from, so the app can build the audio itself
-- (no render queue: Resolve's scripted rendering is unreliable in the free version).
local function audio_map()
  local _, tl = cur()
  if not tl then return { ok = false, error = "Open a timeline in Resolve first." } end
  local info = tl_info()
  local segs = {}
  for t = 1, (tl:GetTrackCount("audio") or 0) do
    local on = true
    pcall(function() on = tl:GetIsTrackEnabled("audio", t) end)
    if on ~= false then
      for _, it in ipairs(tl:GetItemListInTrack("audio", t) or {}) do
        local en = true
        pcall(function() en = it:GetClipEnabled() end)
        local m = it:GetMediaPoolItem()
        local path = m and m:GetClipProperty("File Path")
        if en ~= false and path and path ~= "" then
          segs[#segs + 1] = { path = path, track = t, rec = (it:GetStart() - info.start) / info.fps,
                              dur = it:GetDuration() / info.fps, src = it:GetSourceStartTime() or 0 }
        end
      end
    end
  end
  return { ok = true, segments = segs, info = info }
end

-------------------------------------------------------------------------- commands
local function handle(cmd)
  local op = cmd.op
  if op == "ping" or op == "info" then
    reply(cmd.id, tl_info())
  elseif op == "audiomap" then
    reply(cmd.id, audio_map())
  elseif op == "jump" then
    local _, tl = cur()
    local info = tl_info()
    if tl and info.ok then
      local frame = info.start + math.floor((cmd.seconds or 0) * info.fps + 0.5)
      reply(cmd.id, { ok = tl:SetCurrentTimecode(frames_to_tc(frame, info.fps)) and true or false })
    else
      reply(cmd.id, info)
    end
  elseif op == "render" or op == "insert" or op == "refit" then
    if active then reply(cmd.id, { ok = false, error = "Busy with another task." }) return end
    local step, err = ({ render = job_render, insert = job_insert, refit = job_refit })[op](cmd)
    if not step then reply(cmd.id, { ok = false, error = err }) return end
    active = { id = cmd.id, step = step }
  elseif op == "quit" then
    reply(cmd.id, { ok = true })
    return "quit"
  else
    reply(cmd.id, { ok = false, error = "unknown op " .. tostring(op) })
  end
end

-------------------------------------------------------------------------- main loop
log("MixCaps bridge", VERSION, "started")
writedirect(HB, tostring(os.time()) .. " " .. token)
os.remove(IPC .. SEP .. "cmd.lua")
open_app()

local CMD = IPC .. SEP .. "cmd.lua"
local APPHB = IPC .. SEP .. "app.txt"
local started = os.time()
local lastBeat = 0
local deadChecks = 0
while true do
  local now = os.time()
  if now ~= lastBeat then
    local s = readall(HB)
    if s and not s:find(token, 1, true) then log("another bridge took over; exiting") break end
    writedirect(HB, tostring(now) .. " " .. token)
    lastBeat = now
    local a = readall(APPHB)
    local at = a and tonumber(a:match("^(%d+)"))
    if now - started > 60 and (not at or now - at > 30) then log("app closed; bridge exiting") break end
    -- Resolve gone (closed / restarted): stop, so a fresh bridge can start in the new Resolve
    if now % 15 == 0 then
      if usable(resolve) or live_resolve() then deadChecks = 0 else deadChecks = deadChecks + 1 end
      if deadChecks >= 3 then log("Resolve is gone; bridge exiting") break end
    end
  end
  if exists(CMD) then
    local ok, cmd = pcall(dofile, CMD)
    os.remove(CMD)
    if ok and type(cmd) == "table" then
      local ok2, res = pcall(handle, cmd)
      if not ok2 then log("command failed", tostring(res)); reply(cmd.id or 0, { ok = false, error = tostring(res) }) end
      if res == "quit" then break end
    else
      log("bad command file", tostring(cmd))
    end
  end
  if active then
    local ok, done, res = pcall(active.step)
    if not ok then
      log("task failed", tostring(done))
      reply(active.id, { ok = false, error = tostring(done) })
      active = nil
    elseif done then
      reply(active.id, res)
      active = nil
    end
  end
  sleep(active and 0.05 or 0.2)
end
os.remove(HB)
log("MixCaps bridge stopped")
