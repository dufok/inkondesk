-- inkondesk: draws the pictures the PC sends over one long-lived SSH session
-- (see Screen in xppen_kindle.py). Saves the SSH handshake (~0.3 s) on every redraw.
--
-- Each request:  "<png bytes> <x> <y> <flash 0|1> <cmd bytes>\n", then the shell command
--                (run first, e.g. set the frontlight), then the PNG (drawn at x,y;
--                0 bytes = nothing to draw).
-- Each reply:    "OK\n" or "ERR <what>\n".
local PNG = "/tmp/xpmap.png"
io.stdout:setvbuf("no")

while true do
  local h = io.read("*l")
  if not h then break end
  local n, x, y, flash, clen = h:match("^(%d+) (%d+) (%d+) ([01]) (%d+)$")
  if not n then
    io.write("ERR bad header\n")
    break
  end
  n, clen = tonumber(n), tonumber(clen)
  local cmd = clen > 0 and io.read(clen) or ""
  local data = n > 0 and io.read(n) or ""
  if not cmd or not data or #cmd < clen or #data < n then break end

  if clen > 0 then os.execute(cmd) end
  local ok = true
  if n > 0 then
    local f = io.open(PNG, "wb")
    if f then
      f:write(data)
      f:close()
      local flag = flash == "1" and "-f " or ""
      ok = os.execute("fbink -q " .. flag .. "-g file=" .. PNG .. ",x=" .. x .. ",y=" .. y) == 0
    else
      ok = false
    end
  end
  io.write(ok and "OK\n" or "ERR fbink\n")
end
