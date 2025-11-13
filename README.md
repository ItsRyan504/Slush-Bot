# Roblox Gamepass Price Bot (Discord)

A Discord bot for quickly checking **Roblox gamepass prices**, net earnings after marketplace fees, and basic diagnostics — with support for:

- Scanning by **ID or URL**
- Bulk scanning multiple gamepasses
- Showing how much **Robux you actually receive** after the 30% fee
- Optional detection hints for **regional pricing**
- Per-guild **allowed channel** controls
- Built-in **rate limiting** per user

---

## Project Structure

```text
.
├─ requirements.txt     
└─ src/
   ├─ main.py            
   ├─ keep_alive.py      
   └─ bot/
      ├─ __init__.py     
      └─ allowed_channels.py 