-- VFR Navlog kneeboard for X-Plane 12 (FlyWithLua NG+).
--
-- Shows the plan sent from the VFR Navlog app ("Send to X-Plane") in a floating
-- window: current leg, leg timer on sim time (pauses with the sim), times over
-- waypoints with revised ETAs, checkpoints, notes, altitude changes, the VOR
-- set-up per leg with one-click NAV1/NAV2 tuning, and destination frequencies/ILS.
--
-- Toggle: bind "FlyWithLua/vfr_kneeboard/toggle" to a key or joystick button
-- (Settings > Keyboard / Joystick), or use Plugins > FlyWithLua > Macros.
--
-- Installed and updated by the app (vfr_navlog.api "kneeboard"); edit the copy
-- in the vfr-navlog repo (xplane/vfr_kneeboard.lua), not the installed one.

local PLAN_FILE = SYSTEM_DIRECTORY .. "Output/vfr-navlog/kneeboard_plan.lua"
local LOG_FILE = SYSTEM_DIRECTORY .. "Output/vfr-navlog/kneeboard_log.lua"
local WND_W, WND_H = 470, 640

local kb = {
    wnd = nil,
    plan = nil,        -- table loaded from PLAN_FILE
    stamp = nil,       -- plan.sent_at of the loaded plan
    err = nil,
    ato = {},          -- sim flight time (s) over waypoint i (1-based)
    dest_open = nil,   -- nil = automatic (near destination), true/false = pilot's choice
    near_prev = false,
}

dataref("vfrkb_flight_time", "sim/time/total_flight_time_sec")
dataref("vfrkb_zulu", "sim/time/zulu_time_sec")
dataref("vfrkb_nav1_hz", "sim/cockpit2/radios/actuators/nav1_frequency_hz", "writable")
dataref("vfrkb_nav1_obs", "sim/cockpit2/radios/actuators/nav1_obs_deg_mag_pilot", "writable")
dataref("vfrkb_nav2_hz", "sim/cockpit2/radios/actuators/nav2_frequency_hz", "writable")
dataref("vfrkb_nav2_obs", "sim/cockpit2/radios/actuators/nav2_obs_deg_mag_pilot", "writable")
dataref("vfrkb_nav1_flag", "sim/cockpit2/radios/indicators/nav1_flag_from_to_pilot")
dataref("vfrkb_nav1_dme", "sim/cockpit2/radios/indicators/nav1_dme_distance_nm")

-- ---------------------------------------------------------------------------
-- Formatting helpers

local COL_GREEN = 0xFF80FF80
local COL_RED = 0xFF8080FF
local COL_YELLOW = 0xFF60E0FF
local COL_CYAN = 0xFFFFE080
local COL_DIM = 0xFFB0B0B0

local function text_col(col, s)
    -- tostring first: an error between Push and Pop would leave the colour
    -- stack unbalanced, which corrupts ImGui and can freeze the render thread.
    local str = tostring(s)
    imgui.PushStyleColor(imgui.constant.Col.Text, col)
    imgui.TextUnformatted(str)
    imgui.PopStyleColor()
end

local function scaled(scale, fn)
    local can = imgui.SetWindowFontScale ~= nil
    if can then imgui.SetWindowFontScale(scale) end
    local ok, err = pcall(fn)
    if can then imgui.SetWindowFontScale(1.0) end
    if not ok then error(err) end
end

local function mmss(sec)
    local sign = sec < 0 and "-" or ""
    local s = math.floor(math.abs(sec) + 0.5)
    return string.format("%s%d:%02d", sign, math.floor(s / 60), s % 60)
end

local function zulu(sec)
    sec = sec % 86400
    return string.format("%02d:%02dZ", math.floor(sec / 3600), math.floor(sec / 60) % 60)
end

local function pad3(n)
    return string.format("%03d", math.floor(n + 0.5) % 360)
end

local function wrap(text, width)
    local lines = {}
    for para in (tostring(text or "") .. "\n"):gmatch("(.-)\n") do
        local line = ""
        for word in para:gmatch("%S+") do
            if line == "" then
                line = word
            elseif #line + 1 + #word > width then
                lines[#lines + 1] = line
                line = word
            else
                line = line .. " " .. word
            end
        end
        if line ~= "" then lines[#lines + 1] = line end
    end
    return lines
end

-- ---------------------------------------------------------------------------
-- Geometry (short legs: flat interpolation along the leg, great-circle bearing)

local RAD = math.pi / 180

local function gc_bearing(lat1, lon1, lat2, lon2)
    local f1, f2 = lat1 * RAD, lat2 * RAD
    local dl = (lon2 - lon1) * RAD
    local y = math.sin(dl) * math.cos(f2)
    local x = math.cos(f1) * math.sin(f2) - math.sin(f1) * math.cos(f2) * math.cos(dl)
    return (math.atan2(y, x) / RAD + 360) % 360
end

local function dist_nm(lat1, lon1, lat2, lon2)
    local dx = (lon2 - lon1) * 60 * math.cos(lat1 * RAD)
    local dy = (lat2 - lat1) * 60
    return math.sqrt(dx * dx + dy * dy)
end

-- ---------------------------------------------------------------------------
-- Plan and flight log files

local function read_file(path)
    local f = io.open(path, "r")
    if not f then return nil end
    local text = f:read("*a")
    f:close()
    return text
end

local function load_table(text, name)
    local chunk, err = loadstring(text, name)
    if not chunk then return nil, err end
    setfenv(chunk, {}) -- data only: no access to globals
    local ok, data = pcall(chunk)
    if not ok then return nil, data end
    if type(data) ~= "table" then return nil, "not a table" end
    return data
end

local function save_log()
    if not kb.plan then return end
    local f = io.open(LOG_FILE, "w")
    if not f then return end
    local parts = {}
    for i = 1, #kb.plan.waypoints do
        parts[#parts + 1] = kb.ato[i] and string.format("%.1f", kb.ato[i]) or "false"
    end
    f:write(string.format("return { route_key = %q, ato = { %s } }\n", kb.plan.route_key, table.concat(parts, ", ")))
    f:close()
end

local function load_log()
    kb.ato = {}
    local text = read_file(LOG_FILE)
    if not text or not kb.plan then return end
    local data = load_table(text, "kneeboard_log")
    if not data or data.route_key ~= kb.plan.route_key then return end
    for i, v in ipairs(data.ato or {}) do
        if v and v <= vfrkb_flight_time then kb.ato[i] = v end -- drop times from a previous sim flight
    end
end

function vfrkb_poll()
    local text = read_file(PLAN_FILE)
    if not text then
        kb.err = "No plan yet: press 'Send to X-Plane' in the VFR Navlog app."
        return
    end
    local data, err = load_table(text, "kneeboard_plan")
    if not data then
        kb.err = "Plan file unreadable: " .. tostring(err)
        return
    end
    kb.err = nil
    if data.sent_at == kb.stamp then return end
    local same_route = kb.plan and kb.plan.route_key == data.route_key
    kb.plan = data
    kb.stamp = data.sent_at
    if not same_route then load_log() end -- same route: keep the running flight log
end

-- ---------------------------------------------------------------------------
-- Flight state

local function active_leg()
    -- 0 = not started; i = flying leg i (waypoint i -> i+1); n = arrived.
    if not kb.ato[1] then return 0 end
    local i = 1
    while kb.plan.waypoints[i + 1] and kb.ato[i + 1] do i = i + 1 end
    return i
end

local function mark_over()
    local k = active_leg()
    if k < #kb.plan.waypoints then
        kb.ato[k + 1] = vfrkb_flight_time
        save_log()
    end
end

local function undo_over()
    local k = active_leg()
    if k >= 1 then
        kb.ato[k] = nil
        save_log()
    end
end

local function reset_log()
    kb.ato = {}
    save_log()
end

local function tune(nav, idx)
    if not nav then return end
    if idx == 1 then
        vfrkb_nav1_hz = nav.freq_10khz
        vfrkb_nav1_obs = nav.obs
    else
        vfrkb_nav2_hz = nav.freq_10khz
        vfrkb_nav2_obs = nav.obs
    end
end

-- ---------------------------------------------------------------------------
-- Drawing

local function draw_nav(nav, leg, elapsed)
    if not nav then return end
    imgui.Separator()
    if nav.trackable then
        text_col(COL_GREEN, string.format("NAV  %s %s   OBS %s %s", nav.ident, nav.freq, pad3(nav.obs), nav.flag))
        text_col(COL_DIM, string.format("     %s, needle within %d deg%s", nav.radial_label, math.max(1, math.floor(nav.max_dev + 0.5)),
            nav.suggested and "  (suggested)" or ""))
    else
        text_col(COL_CYAN, string.format("NAV  %s %s   R%s -> R%s", nav.ident, nav.freq, pad3(nav.r_start), pad3(nav.r_end)))
        text_col(COL_DIM, "     not along a radial: progress check" .. (nav.suggested and "  (suggested)" or ""))
    end
    if elapsed then
        -- Where the clock says we are: the radial and DME to expect now.
        local a = kb.plan.waypoints[leg.from_idx]
        local b = kb.plan.waypoints[leg.from_idx + 1]
        local frac = math.min(1, (leg.gs * elapsed / 3600) / math.max(leg.dist, 0.1))
        local lat = a.lat + (b.lat - a.lat) * frac
        local lon = a.lon + (b.lon - a.lon) * frac
        local radial = (gc_bearing(nav.lat, nav.lon, lat, lon) - nav.var + 360) % 360
        local line = "     expect now R" .. pad3(radial)
        if nav.dme then line = line .. string.format("  DME %.1f", dist_nm(nav.lat, nav.lon, lat, lon)) end
        imgui.TextUnformatted(line)
    end
    if imgui.Button("Tune NAV1", 110, 24) then tune(nav, 1) end
    imgui.SameLine()
    if imgui.Button("Tune NAV2", 110, 24) then tune(nav, 2) end
    if vfrkb_nav1_hz == nav.freq_10khz then
        imgui.SameLine()
        local flag = ({ [0] = "OFF", [1] = "TO", [2] = "FROM" })[vfrkb_nav1_flag] or "?"
        local dme = (vfrkb_nav1_dme and vfrkb_nav1_dme > 0) and string.format("  DME %.1f", vfrkb_nav1_dme) or ""
        text_col(COL_DIM, "  NAV1: " .. flag .. dme)
    end
end

local function draw_destination()
    local d = kb.plan.dest
    if not d then return end
    imgui.Separator()
    text_col(COL_CYAN, d.ident .. (d.name ~= "" and ("  " .. d.name) or "") .. (d.elev and string.format("   elev %d ft", d.elev) or ""))
    for _, f in ipairs(d.freqs or {}) do
        imgui.TextUnformatted(string.format("  %-10s %s%s", f.label, f.freq, f.live and "  (VATSIM)" or ""))
    end
    for _, ils in ipairs(d.ils or {}) do
        text_col(COL_GREEN, "  " .. ils)
    end
    for _, rw in ipairs(d.runways or {}) do
        text_col(COL_DIM, "  RWY " .. rw)
    end
    if d.atis and #d.atis > 0 then
        text_col(COL_DIM, "  ATIS (" .. (d.as_of or "") .. "):")
        for _, l in ipairs(wrap(table.concat(d.atis, " "), 60)) do imgui.TextUnformatted("    " .. l) end
    end
    if d.metar then
        text_col(COL_DIM, "  METAR (" .. (d.as_of or "") .. "):")
        for _, l in ipairs(wrap(d.metar, 60)) do imgui.TextUnformatted("    " .. l) end
    end
end

local function draw_eta_table(k, factor)
    imgui.Separator()
    local t0 = kb.ato[k]
    local zulu0 = vfrkb_zulu - (vfrkb_flight_time - t0) -- zulu at the last time over
    local planned, revised = 0, 0
    for j = k, #kb.plan.legs do
        planned = planned + kb.plan.legs[j].ete_min * 60
        revised = revised + kb.plan.legs[j].ete_min * 60 * factor
        local wp = kb.plan.waypoints[j + 1]
        local line = string.format("  %-16s ETA %s   rev %s", wp.ident:sub(1, 16), zulu(zulu0 + planned), zulu(zulu0 + revised))
        if j == k then text_col(COL_YELLOW, line) else imgui.TextUnformatted(line) end
    end
end

local function draw_plan()
    local p = kb.plan
    local n = #p.waypoints
    local k = active_leg()
    text_col(COL_DIM, p.title .. "   " .. zulu(vfrkb_zulu))

    local near = k >= 1 and (k >= n - 1 or (p.call_leg and k >= p.call_leg))
    if near ~= kb.near_prev then kb.dest_open = nil; kb.near_prev = near end
    local dest_open = kb.dest_open
    if dest_open == nil then dest_open = near end

    if k == 0 then
        local leg = p.legs[1]
        scaled(1.5, function()
            imgui.TextUnformatted(string.format("Ready  %s -> %s", leg.from, leg.to))
            imgui.TextUnformatted(string.format("MH %s   ALT %d", pad3(leg.mh), leg.alt))
        end)
        if imgui.Button("Start: over " .. leg.from .. " now", 300, 34) then mark_over() end
        draw_nav(leg.nav, leg, nil)
    elseif k >= n then
        scaled(1.5, function() imgui.TextUnformatted("Arrived " .. p.waypoints[n].ident) end)
        imgui.TextUnformatted("Block time " .. mmss(kb.ato[n] - kb.ato[1]))
        dest_open = true
    else
        local leg = p.legs[k]
        local elapsed = vfrkb_flight_time - kb.ato[k]
        local remaining = leg.ete_min * 60 - elapsed
        scaled(1.3, function()
            imgui.TextUnformatted(string.format("%s -> %s   (leg %d/%d)", leg.from, leg.to, k, n - 1))
        end)
        scaled(2.0, function()
            imgui.TextUnformatted(string.format("MH %s   %d ft", pad3(leg.mh), leg.alt))
        end)
        imgui.TextUnformatted(string.format("GS %d kt   DIST %.1f NM   ETE %s", leg.gs, leg.dist, mmss(leg.ete_min * 60)))
        scaled(1.8, function()
            text_col(remaining < 0 and COL_RED or COL_YELLOW,
                string.format("%s %s   ETO %s", remaining < 0 and "overdue" or ("to " .. leg.to), mmss(remaining),
                    zulu(vfrkb_zulu + remaining)))
        end)

        local next_leg = p.legs[k + 1]
        if next_leg and next_leg.alt ~= leg.alt then
            text_col(COL_YELLOW, string.format("At %s: %s to %d ft", leg.to, next_leg.alt > leg.alt and "climb" or "descend", next_leg.alt))
        end

        if imgui.Button("Over " .. leg.to .. " now", 260, 34) then mark_over() end
        imgui.SameLine()
        if imgui.Button("Undo", 70, 34) then undo_over() end

        draw_nav(leg.nav, leg, elapsed)
        if next_leg and next_leg.nav then
            local nn = next_leg.nav
            text_col(COL_DIM, string.format("Next leg: %s %s %s", nn.ident, nn.freq,
                nn.trackable and ("OBS " .. pad3(nn.obs) .. " " .. nn.flag) or ("R" .. pad3(nn.r_start) .. "->R" .. pad3(nn.r_end))))
        end

        if leg.checkpoints and #leg.checkpoints > 0 then
            imgui.Separator()
            for _, cp in ipairs(leg.checkpoints) do
                local dt = cp.min * 60 - elapsed
                local col = dt < -30 and COL_DIM or (dt < 60 and COL_YELLOW or 0xFFFFFFFF)
                text_col(col, string.format("  %s   %s", cp.label, dt >= 0 and ("in " .. mmss(dt)) or "passed"))
            end
        end

        local wp = p.waypoints[k + 1]
        if (wp.notes and wp.notes ~= "") or (wp.fixes and #wp.fixes > 0) then
            imgui.Separator()
            text_col(COL_DIM, "Notes " .. wp.ident)
            for _, l in ipairs(wrap(wp.notes, 62)) do imgui.TextUnformatted("  " .. l) end
            for _, fx in ipairs(wp.fixes or {}) do text_col(COL_DIM, "  " .. fx) end
        end

        -- Revised ETAs scale the remaining legs by the last leg's actual/planned time.
        local factor = 1
        if k >= 2 then
            local actual = kb.ato[k] - kb.ato[k - 1]
            local plan_s = p.legs[k - 1].ete_min * 60
            if plan_s > 0 then factor = actual / plan_s end
        end
        draw_eta_table(k, factor)
    end

    imgui.Separator()
    if imgui.Button((dest_open and "Hide " or "Show ") .. "destination " .. p.waypoints[n].ident, 230, 24) then
        kb.dest_open = not dest_open
    end
    if k >= 1 then
        imgui.SameLine()
        if imgui.Button("Reset log", 90, 24) then reset_log() end
    end
    if dest_open then draw_destination() end
end

function vfrkb_build_inner(wnd, x, y)
    if kb.err and not kb.plan then
        text_col(COL_YELLOW, kb.err)
        return
    end
    if kb.err then text_col(COL_RED, kb.err) end
    draw_plan()
end

function vfrkb_build(wnd, x, y)
    -- Never let a draw error escape into FlyWithLua's render path.
    local ok, err = pcall(vfrkb_build_inner, wnd, x, y)
    if not ok then imgui.TextUnformatted("Kneeboard draw error (safe): " .. tostring(err)) end
end

-- ---------------------------------------------------------------------------
-- Window

-- Window placement. Positions are in X-Plane UI units ("boxels"), not pixels:
-- at 150% UI scaling a 5120x1440 monitor is 3413x960 boxels, so SCREEN_WIDTH /
-- SCREEN_HEIGHT (pixels) cannot be used to place a window. The main monitor's
-- real bounds come from XPLMGetAllMonitorBoundsGlobal; the window is placed with
-- float_wnd_set_geometry (explicit left/top/right/bottom, y grows upwards).
-- Where the pilot drags the window is remembered for the next toggle.

local function main_monitor()
    if XPLMGetAllMonitorBoundsGlobal == nil then return nil end
    local ok, mons = pcall(XPLMGetAllMonitorBoundsGlobal)
    if not ok or type(mons) ~= "table" or #mons == 0 then return nil end
    local best = mons[1]
    for _, m in ipairs(mons) do
        if m.inLeft == 0 then best = m end -- the monitor at the origin is X-Plane's main one
    end
    return best
end

local function default_geometry()
    local m = main_monitor()
    if not m then return nil end
    local h = math.min(WND_H, (m.inTop - m.inBottom) - 100)
    local left = m.inLeft + 40
    local top = m.inTop - 60 -- below X-Plane's menu bar
    return { left, top, left + WND_W, top - h }
end

local function remember_geometry()
    if kb.wnd == nil or float_wnd_get_geometry == nil then return end
    local ok, l, t, r, b = pcall(float_wnd_get_geometry, kb.wnd)
    if ok and l and t and r and b then kb.geom = { l, t, r, b } end
end

function vfrkb_show()
    if kb.wnd ~= nil then return end
    kb.wnd = float_wnd_create(WND_W, WND_H, 1, true)
    float_wnd_set_title(kb.wnd, "VFR Kneeboard")
    local g = kb.geom or default_geometry()
    if g and float_wnd_set_geometry ~= nil then
        float_wnd_set_geometry(kb.wnd, g[1], g[2], g[3], g[4])
        logMsg(string.format("VFR kneeboard: window l=%d t=%d r=%d b=%d", g[1], g[2], g[3], g[4]))
    else
        -- Fallback: float_wnd_set_position takes left and BOTTOM. Keep it low
        -- enough to fit even at 200% UI scaling.
        float_wnd_set_position(kb.wnd, 60, 40)
        logMsg("VFR kneeboard: window at left=60 bottom=40 (no monitor bounds)")
    end
    float_wnd_set_imgui_builder(kb.wnd, "vfrkb_build")
    float_wnd_set_onclose(kb.wnd, "vfrkb_on_close")
end

function vfrkb_on_close(wnd)
    remember_geometry()
    kb.wnd = nil
end

function vfrkb_toggle()
    if kb.wnd ~= nil then
        remember_geometry()
        float_wnd_destroy(kb.wnd)
        kb.wnd = nil
    else
        vfrkb_show()
    end
end

function vfrkb_reset_position()
    kb.geom = nil
    if kb.wnd ~= nil then
        float_wnd_destroy(kb.wnd)
        kb.wnd = nil
    end
    vfrkb_show()
end

function vfrkb_over_now()
    if kb.plan then mark_over() end
end

vfrkb_poll()
do_often("vfrkb_poll()")
add_macro("VFR Kneeboard: toggle window", "vfrkb_toggle()")
add_macro("VFR Kneeboard: reset window position", "vfrkb_reset_position()")
create_command("FlyWithLua/vfr_kneeboard/toggle", "Toggle the VFR kneeboard window", "vfrkb_toggle()", "", "")
create_command("FlyWithLua/vfr_kneeboard/over_waypoint", "VFR kneeboard: over the next waypoint now", "vfrkb_over_now()", "", "")
logMsg("VFR kneeboard loaded (plan: " .. PLAN_FILE .. ")")
