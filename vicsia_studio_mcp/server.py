"""Vicsia Studio MCP Server v2 - Admin/Construction.

MCP server for managing and configuring Vicsia agents, groups, and connectors.

CONCEPTS:
- Agent: unite vocale — name + prompt suffisent, tout le reste est auto-derive
  (hotkey, description, routing_keywords, llm_tier).
- Groupe: orchestrateur qui route la voix vers le bon agent enfant.
  Un connecteur (MCP) assigne au groupe est herite par ses agents.
- Connecteur (MCP): integration externe (Gmail, Outlook, Slack, Stripe...).
  Installer depuis le catalogue, configurer, activer.
- Pack: ensemble pre-configure d'agents + orchestrateur (ex: base, mail).

CREATION SIMPLIFIEE:
- create_agent: seul le nom est requis. voice=true (vocal) ou false (action sur selection).
- create_group: nom + optionnellement des agents inline. Cree l'orchestrateur + les enfants.
- install_package: installe un pack complet en un appel.

OUTILS (30):
- Agents      : create_agent, create_group, update_agent, delete_agent, list_agents, get_agent
- Lifecycle   : archive_agent, restore_agent, set_favorite
- Groupes     : list_groups, add_to_group, remove_from_group, install_package, list_packages
- Connecteurs : list_mcps, get_mcp, toggle_mcp, add_mcp, delete_mcp,
                browse_mcp_library, install_mcp, get_mcp_config, configure_mcp,
                start_mcp_auth, poll_mcp_auth
- Settings    : get_settings, update_settings
- Introspection: get_vicsia_status, list_profiles, get_last_result
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp

# MCP SDK
try:
    from mcp.server.lowlevel import NotificationOptions, Server
    from mcp.server.models import InitializationOptions
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    print("MCP SDK not installed. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)


# Vicsia API (Flask server)
def _get_vicsia_api_url() -> str:
    """Discover Vicsia API URL: VICSIA_PORT env var -> port file -> fallback 5123."""
    if port_str := os.environ.get("VICSIA_PORT"):
        try:
            return f"http://localhost:{int(port_str)}"
        except ValueError:
            pass

    candidates: list[Path] = []

    if sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "Vicsia" / ".vicsia_port")
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        candidates.append(Path(appdata) / "Vicsia" / ".vicsia_port")
    else:
        candidates.append(Path.home() / ".local" / "share" / "Vicsia" / ".vicsia_port")
    candidates.append(Path(__file__).parent.parent / "data" / ".vicsia_port")  # dev

    for p in candidates:
        if p.exists():
            try:
                return f"http://localhost:{int(p.read_text().strip())}"
            except (ValueError, OSError):
                pass

    return "http://localhost:5123"


VICSIA_API_URL = _get_vicsia_api_url()


def _get_vicsia_csrf_token() -> str:
    """Discover CSRF token: env var -> token file -> empty."""
    if token := os.environ.get("VICSIA_CSRF_TOKEN"):
        return token

    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "Vicsia" / ".vicsia_csrf")
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        candidates.append(Path(appdata) / "Vicsia" / ".vicsia_csrf")
    else:
        candidates.append(Path.home() / ".local" / "share" / "Vicsia" / ".vicsia_csrf")
    candidates.append(Path(__file__).parent.parent / "data" / ".vicsia_csrf")  # dev

    for p in candidates:
        if p.exists():
            try:
                return p.read_text().strip()
            except OSError:
                pass
    return ""


_csrf_token: str | None = None


async def _call_api(method: str, path: str, data: dict | None = None) -> dict:
    """Appelle l'API Vicsia via HTTP avec CSRF token sur mutations."""
    global _csrf_token
    if _csrf_token is None:
        _csrf_token = _get_vicsia_csrf_token()

    url = f"{VICSIA_API_URL}{path}"
    headers: dict[str, str] = {}
    if method in ("POST", "PUT", "DELETE", "PATCH"):
        headers["X-Vicsia-Token"] = _csrf_token

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.request(method, url, json=data, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp,
        ):
            body = await resp.json()
            # Retry once on CSRF rejection (MCP started before Vicsia)
            if resp.status == 403 and isinstance(body, dict) and "Token invalide" in body.get("error", ""):
                _csrf_token = _get_vicsia_csrf_token()
                if _csrf_token:
                    headers["X-Vicsia-Token"] = _csrf_token
                    async with session.request(
                        method, url, json=data, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
                    ) as retry:
                        return await retry.json()
            return body
    except aiohttp.ClientConnectorError:
        return {"ok": False, "error": "Vicsia n'est pas lance. Demarrez l'application Vicsia d'abord."}
    except TimeoutError:
        return {"ok": False, "error": "Timeout: L'API Vicsia ne repond pas."}
    except Exception as e:
        return {"ok": False, "error": f"Erreur API: {str(e)}"}


def _json_result(result) -> list[TextContent]:
    """Encode le resultat en JSON pour le retour MCP."""
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


# ============================================================================
# Tools definitions
# ============================================================================


def get_tools() -> list[Tool]:
    """Retourne la liste des outils disponibles."""
    return [
        # === Agents ===
        Tool(
            name="list_agents",
            description=(
                "Liste tous les agents Vicsia. Les groupes sont des agents avec is_orchestrator=true, "
                "orchestrator_scope contient les IDs des agents enfants."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_archived": {"type": "boolean", "description": "Inclure les archives (defaut: true)"},
                    "summary": {
                        "type": "boolean",
                        "description": "Vue compacte: id, name, hotkey, enabled, archived, is_orchestrator (defaut: false)",
                    },
                },
            },
        ),
        Tool(
            name="get_agent",
            description="Details d'un agent par son ID",
            inputSchema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="create_agent",
            description=(
                "Cree un agent Vicsia. Seul le nom est requis — tout le reste est auto-derive "
                "(hotkey, description, routing_keywords, llm_tier). "
                "voice=true (defaut): agent vocal (micro -> LLM -> sortie). "
                "voice=false: action directe sur texte selectionne (raccourci sans micro, non orchestrable). "
                "group_id: ajoute directement l'agent dans un groupe existant."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nom de l'agent (max 200 chars)"},
                    "system_prompt": {"type": "string", "description": "Instructions pour l'agent"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["paste", "capsule"],
                        "description": "Mode de sortie (defaut: capsule). paste=colle dans l'app active, capsule=affiche dans la mini-capsule Vicsia.",
                    },
                    "tts_enabled": {
                        "type": "boolean",
                        "description": "Lecture vocale du resultat en plus de la capsule (defaut: false). Incompatible avec paste.",
                    },
                    "voice": {
                        "type": "boolean",
                        "description": "true (defaut): commande vocale. false: action directe sur selection.",
                    },
                    "selection": {
                        "type": "boolean",
                        "description": "Capturer le texte selectionne comme contexte (defaut: false)",
                    },
                    "web_search": {"type": "boolean", "description": "Activer la recherche web (defaut: false)"},
                    "memory": {"type": "boolean", "description": "Activer la memoire persistante (defaut: false)"},
                    "group_id": {
                        "type": "string",
                        "description": "ID d'un groupe existant — l'agent sera ajoute a son scope",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="create_group",
            description=(
                "Cree un groupe d'agents (orchestrateur) qui route les commandes vocales vers le bon agent enfant. "
                "Un connecteur (MCP) peut etre assigne au groupe — il est automatiquement herite par les agents enfants. "
                "agents: liste optionnelle de definitions d'agents a creer et ajouter au groupe. "
                "Les champs de securite (write_mode, blocked_tools, max_writes) se definissent au niveau du GROUPE "
                "et sont propages automatiquement a tous les agents enfants."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nom du groupe (ex: Email, Compta, CRM)"},
                    "description": {"type": "string", "description": "A quoi sert ce groupe"},
                    "mcp": {"type": "string", "description": "ID du connecteur herite par les agents du groupe"},
                    "write_mode": {
                        "type": "string",
                        "enum": ["none", "ask", "auto"],
                        "description": (
                            "Strategie d'ecriture pour tous les agents du groupe (defaut: none). "
                            "none=lecture seule, ask=confirmation avant ecriture, auto=ecritures automatiques. "
                            "Propage aux agents enfants."
                        ),
                    },
                    "blocked_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Patterns de tools interdits pour tous les agents du groupe (ex: ['send_*', 'delete_*']). "
                            "Propage aux agents enfants."
                        ),
                    },
                    "max_writes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 6,
                        "description": "Nombre max d'ecritures par session (defaut: 1). Propage aux agents enfants.",
                    },
                    "agents": {
                        "type": "array",
                        "description": "Agents a creer dans le groupe",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "system_prompt": {"type": "string"},
                                "output_mode": {
                                    "type": "string",
                                    "enum": ["paste", "capsule"],
                                },
                                "voice": {"type": "boolean"},
                                "selection": {"type": "boolean"},
                                "web_search": {"type": "boolean", "description": "Activer la recherche web"},
                                "memory": {"type": "boolean", "description": "Activer la memoire persistante"},
                            },
                            "required": ["name"],
                        },
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="update_agent",
            description="Met a jour un agent existant. Seuls les champs fournis sont modifies. Pour les parametres avances non exposes par create_agent.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "name": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "description": {"type": "string"},
                    "mcps": {"type": "array", "items": {"type": "string"}},
                    "hotkey": {"type": "string"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["paste", "capsule"],
                    },
                    "tts_enabled": {
                        "type": "boolean",
                        "description": "Lecture vocale du resultat en plus de la capsule (capsule uniquement)",
                    },
                    "enabled": {"type": "boolean"},
                    "requires_voice": {"type": "boolean"},
                    "capture_selection": {"type": "boolean"},
                    "capture_screenshot": {"type": "string", "enum": ["never", "auto", "always"]},
                    "capture_window": {"type": "boolean"},
                    "web_search_enabled": {"type": "boolean"},
                    "mcp_accessible": {"type": "boolean"},
                    "orchestrable": {"type": "boolean"},
                    "routing_keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                    "is_orchestrator": {"type": "boolean"},
                    "orchestrator_mode": {"type": "string", "enum": ["router", "multi"]},
                    "orchestrator_scope": {"type": "array", "items": {"type": "string"}},
                    "orchestrator_agent_output": {"type": "string", "enum": ["return", "display", "libre"]},
                    "write_mode": {"type": "string", "enum": ["none", "ask", "auto"]},
                    "max_writes": {"type": "integer", "minimum": 1, "maximum": 6},
                    "blocked_tools": {"type": "array", "items": {"type": "string"}},
                    "allowed_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Whitelist de tools visibles (prioritaire sur blocked_tools). Vide = tous les tools.",
                    },
                    "mcp_permissions": {"type": "object"},
                    "category": {"type": "string"},
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="delete_agent",
            description="Supprime un agent ou un groupe",
            inputSchema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
        ),
        # === Lifecycle ===
        Tool(
            name="archive_agent",
            description="Archive un agent (desactive le raccourci, l'agent reste accessible via MCP)",
            inputSchema={"type": "object", "properties": {"agent_id": {"type": "string"}}, "required": ["agent_id"]},
        ),
        Tool(
            name="restore_agent",
            description="Restaure un agent archive (reactive le raccourci)",
            inputSchema={"type": "object", "properties": {"agent_id": {"type": "string"}}, "required": ["agent_id"]},
        ),
        Tool(
            name="set_favorite",
            description="Toggle le statut favori d'un agent",
            inputSchema={"type": "object", "properties": {"agent_id": {"type": "string"}}, "required": ["agent_id"]},
        ),
        Tool(
            name="get_available_hotkeys",
            description="Retourne les raccourcis clavier disponibles",
            inputSchema={"type": "object", "properties": {}},
        ),
        # === Groupes ===
        Tool(
            name="list_groups",
            description="Liste les groupes (orchestrateurs) avec leurs agents enfants",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="add_to_group",
            description="Ajoute un agent a un groupe existant. L'agent doit avoir requires_voice=true.",
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "ID du groupe (orchestrateur)"},
                    "agent_id": {"type": "string", "description": "ID de l'agent a ajouter"},
                },
                "required": ["group_id", "agent_id"],
            },
        ),
        Tool(
            name="remove_from_group",
            description="Retire un agent d'un groupe",
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                },
                "required": ["group_id", "agent_id"],
            },
        ),
        Tool(
            name="list_packages",
            description="Liste les packs d'agents pre-configures disponibles (base, mail, etc.)",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="install_package",
            description="Installe un pack complet d'agents + orchestrateur. Ex: 'base' (7 agents), 'mail' (3 agents email + orchestrateur).",
            inputSchema={
                "type": "object",
                "properties": {"package_id": {"type": "string", "description": "ID du pack (ex: base, mail)"}},
                "required": ["package_id"],
            },
        ),
        # === Connecteurs (MCPs) ===
        Tool(
            name="list_mcps",
            description="Liste les connecteurs installes avec leur statut (actif/inactif)",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_mcp",
            description="Details d'un connecteur par son ID",
            inputSchema={"type": "object", "properties": {"mcp_id": {"type": "string"}}, "required": ["mcp_id"]},
        ),
        Tool(
            name="browse_mcp_library",
            description="Parcourt le catalogue des connecteurs disponibles a l'installation, par categorie ou recherche textuelle.",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filtrer par categorie (email, crm, billing, browser...)",
                    },
                    "search": {
                        "type": "string",
                        "description": "Recherche par nom ou description (ex: 'qonto', 'email')",
                    },
                },
            },
        ),
        Tool(
            name="install_mcp",
            description=(
                "Installe un connecteur depuis le catalogue. "
                "Si le retour contient config_fields, demandez les valeurs a l'utilisateur puis appelez configure_mcp. "
                "Si auth_type est 'oauth', appelez start_mcp_auth."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mcp_id": {
                        "type": "string",
                        "description": "ID du connecteur (ex: google-workspace, slack, stripe)",
                    }
                },
                "required": ["mcp_id"],
            },
        ),
        Tool(
            name="get_mcp_config",
            description="Obtient le formulaire de configuration d'un connecteur (champs requis, valeurs actuelles, statut OAuth)",
            inputSchema={
                "type": "object",
                "properties": {"mcp_id": {"type": "string"}},
                "required": ["mcp_id"],
            },
        ),
        Tool(
            name="configure_mcp",
            description="Sauvegarde la configuration d'un connecteur (API key, dossiers, etc.). Utiliser get_mcp_config d'abord pour voir les champs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcp_id": {"type": "string"},
                    "values": {
                        "type": "object",
                        "description": "Valeurs de config (ex: {api_key: 'sk-...', folders: ['/path']})",
                    },
                },
                "required": ["mcp_id", "values"],
            },
        ),
        Tool(
            name="start_mcp_auth",
            description="Lance le flow OAuth pour un connecteur (Microsoft: retourne device_code a entrer sur microsoft.com/devicelogin. Google: lazy auth au premier usage).",
            inputSchema={
                "type": "object",
                "properties": {"mcp_id": {"type": "string"}},
                "required": ["mcp_id"],
            },
        ),
        Tool(
            name="poll_mcp_auth",
            description="Verifie si l'authentification OAuth est completee (Microsoft). Appeler toutes les 5s apres start_mcp_auth. Status: pending/success/expired/declined.",
            inputSchema={
                "type": "object",
                "properties": {"mcp_id": {"type": "string"}},
                "required": ["mcp_id"],
            },
        ),
        Tool(
            name="toggle_mcp",
            description="Active ou desactive un connecteur",
            inputSchema={
                "type": "object",
                "properties": {
                    "mcp_id": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["mcp_id", "enabled"],
            },
        ),
        Tool(
            name="add_mcp",
            description="Ajoute un connecteur custom (commande + args + env)",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "command": {"type": "string", "description": "Commande (npx, uvx, python...)"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "env": {"type": "object"},
                    "description": {"type": "string"},
                },
                "required": ["name", "command"],
            },
        ),
        Tool(
            name="delete_mcp",
            description="Supprime un connecteur custom",
            inputSchema={"type": "object", "properties": {"mcp_id": {"type": "string"}}, "required": ["mcp_id"]},
        ),
        # === Settings ===
        Tool(
            name="get_settings",
            description="Configuration globale Vicsia (langue, audio, modeles, permissions)",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="update_settings",
            description="Met a jour la configuration globale. Seuls les champs fournis sont modifies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "language": {"type": "string", "enum": ["fr", "en", "auto"]},
                    "audio": {"type": "object"},
                    "models": {"type": "object"},
                    "permissions": {"type": "object"},
                },
            },
        ),
        # === Introspection ===
        Tool(
            name="get_vicsia_status",
            description="Etat actuel de Vicsia (pause, recording, etc.)",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_profiles",
            description="Liste les profils vocaux disponibles",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_last_result",
            description="Dernieres actions Vicsia (input, output, mode, duree, succes)",
            inputSchema={
                "type": "object",
                "properties": {"n": {"type": "integer", "description": "Nombre de resultats (defaut: 1, max: 10)"}},
            },
        ),
        Tool(
            name="reload_config",
            description="Force le rechargement des agents et connecteurs. Utile apres des modifications manuelles.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="batch_delete",
            description="Supprime plusieurs agents en un appel. Maximum 20 agents par batch.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 20,
                        "description": "Liste des IDs d'agents a supprimer",
                    },
                },
                "required": ["agent_ids"],
            },
        ),
    ]


# ============================================================================
# Tool dispatch
# ============================================================================


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute un outil et retourne le resultat."""

    handlers = {
        # Agents
        "list_agents": lambda: _list_agents(arguments.get("include_archived", True), arguments.get("summary", False)),
        "get_agent": lambda: _get_agent(arguments["agent_id"]),
        "create_agent": lambda: _create_agent(arguments),
        "create_group": lambda: _create_group(arguments),
        "update_agent": lambda: _update_agent(arguments),
        "delete_agent": lambda: _delete_agent(arguments["agent_id"]),
        # Lifecycle
        "archive_agent": lambda: _api_post(f"/api/agents/{arguments['agent_id']}/archive"),
        "restore_agent": lambda: _api_post(f"/api/agents/{arguments['agent_id']}/restore"),
        "set_favorite": lambda: _api_post(f"/api/agents/{arguments['agent_id']}/favorite"),
        "get_available_hotkeys": lambda: _api_get("/api/agents/hotkeys"),
        # Groupes
        "list_groups": lambda: _list_groups(),
        "add_to_group": lambda: _add_to_group(arguments["group_id"], arguments["agent_id"]),
        "remove_from_group": lambda: _remove_from_group(arguments["group_id"], arguments["agent_id"]),
        "list_packages": lambda: _api_get("/api/agents/library"),
        "install_package": lambda: _api_post(f"/api/agents/install-package/{arguments['package_id']}"),
        # Connecteurs
        "list_mcps": lambda: _list_mcps(),
        "get_mcp": lambda: _api_get(f"/api/mcps/{arguments['mcp_id']}"),
        "browse_mcp_library": lambda: _browse_mcp_library(arguments.get("category"), arguments.get("search")),
        "install_mcp": lambda: _install_mcp(arguments["mcp_id"]),
        "get_mcp_config": lambda: _api_get(f"/api/mcps/{arguments['mcp_id']}/config-schema"),
        "configure_mcp": lambda: _api_put(f"/api/mcps/{arguments['mcp_id']}/config", arguments["values"]),
        "start_mcp_auth": lambda: _api_post(f"/api/mcps/{arguments['mcp_id']}/auth"),
        "poll_mcp_auth": lambda: _api_post(f"/api/mcps/{arguments['mcp_id']}/auth/poll"),
        "toggle_mcp": lambda: _api_post(f"/api/mcps/{arguments['mcp_id']}/toggle", {"enabled": arguments["enabled"]}),
        "add_mcp": lambda: _add_mcp(arguments),
        "delete_mcp": lambda: _api_delete(f"/api/mcps/{arguments['mcp_id']}"),
        # Settings
        "get_settings": lambda: _api_get("/api/settings"),
        "update_settings": lambda: _api_put("/api/settings", arguments),
        # Introspection
        "get_vicsia_status": lambda: _api_get("/api/status"),
        "list_profiles": lambda: _api_get("/api/profiles"),
        "get_last_result": lambda: _get_last_result(arguments.get("n", 1)),
        "reload_config": lambda: _reload_config(),
        "batch_delete": lambda: _batch_delete(arguments["agent_ids"]),
    }

    handler = handlers.get(name)
    if handler:
        return await handler()
    return _json_result({"ok": False, "error": f"Unknown tool: {name}"})


# ============================================================================
# API helpers (simple proxy)
# ============================================================================


async def _api_get(path: str) -> list[TextContent]:
    return _json_result(await _call_api("GET", path))


async def _api_post(path: str, data: dict | None = None) -> list[TextContent]:
    return _json_result(await _call_api("POST", path, data))


async def _api_put(path: str, data: dict | None = None) -> list[TextContent]:
    return _json_result(await _call_api("PUT", path, data))


async def _api_delete(path: str) -> list[TextContent]:
    return _json_result(await _call_api("DELETE", path))


# ============================================================================
# Agents
# ============================================================================


async def _list_agents(include_archived: bool = True, summary: bool = False) -> list[TextContent]:
    param = "true" if include_archived else "false"
    result = await _call_api("GET", f"/api/agents?include_archived={param}")
    # API returns bare list — normalize; propagate errors
    if isinstance(result, list):
        agents = result
    elif isinstance(result, dict) and "error" in result:
        return _json_result(result)
    else:
        agents = result.get("agents", [])
    if summary:
        agents = [
            {k: a[k] for k in ("id", "name", "enabled", "archived", "is_orchestrator", "favorite", "hotkey") if k in a}
            for a in agents
        ]
    return _json_result({"ok": True, "agents": agents, "count": len(agents)})


async def _get_agent(agent_id: str) -> list[TextContent]:
    return _json_result(await _call_api("GET", f"/api/agents/{agent_id}"))


async def _create_agent(args: dict) -> list[TextContent]:
    voice = args.get("voice", True)
    data = {
        "name": args["name"],
        "system_prompt": args.get("system_prompt", ""),
        "output_mode": args.get("output_mode", "capsule"),
        "tts_enabled": args.get("tts_enabled", False),
        "requires_voice": voice,
        "capture_selection": args.get("selection", False),
        "capture_window": voice,
        "capture_screenshot": "auto" if voice else "never",
        "llm_tier": "thinking",
        "web_search_enabled": args.get("web_search", False),
        "memory_enabled": args.get("memory", False),
    }
    result = await _call_api("POST", "/api/agents", data)

    # Si group_id fourni et creation reussie, ajouter au groupe
    group_id = args.get("group_id")
    if group_id and result.get("ok"):
        agent_id = result.get("agent", {}).get("id")
        if agent_id:
            await _add_to_group_impl(group_id, agent_id)

    return _json_result(result)


async def _create_group(args: dict) -> list[TextContent]:
    mcp_id = args.get("mcp", "")

    # Champs de securite definis au niveau groupe, propages aux enfants
    group_write_mode = args.get("write_mode", "none")
    group_blocked_tools = args.get("blocked_tools", [])
    group_max_writes = args.get("max_writes", 1)

    # 1. Creer l'orchestrateur
    group_data = {
        "name": args["name"],
        "description": args.get("description", ""),
        "system_prompt": "",
        "output_mode": "capsule",
        "is_orchestrator": True,
        "orchestrator_mode": "router",
        "orchestrator_scope": [],
        "mcps": [mcp_id] if mcp_id else [],
        "requires_voice": True,
        "capture_selection": True,
        "capture_window": True,
        "capture_screenshot": "auto",
        "llm_tier": "thinking",
        "write_mode": group_write_mode,
        "blocked_tools": group_blocked_tools,
        "max_writes": group_max_writes,
    }
    group_result = await _call_api("POST", "/api/agents", group_data)
    if not group_result.get("ok"):
        return _json_result(group_result)

    group_id = group_result.get("agent", {}).get("id")
    created_agents = []
    reused_agents = []
    failed_agents = []

    # 2. Index des agents existants pour eviter les doublons
    existing = await _call_api("GET", "/api/agents?include_archived=false")
    name_to_id: dict[str, str] = {}
    if isinstance(existing, list):
        name_to_id = {a["name"].lower(): a["id"] for a in existing if a.get("name")}

    # 3. Creer les agents enfants (heritent du connecteur)
    for agent_def in args.get("agents", []):
        # Reutiliser un agent existant avec le meme nom
        existing_id = name_to_id.get(agent_def["name"].lower())
        if existing_id:
            created_agents.append(existing_id)
            reused_agents.append(agent_def["name"])
            continue

        voice = agent_def.get("voice", True)
        agent_data = {
            "name": agent_def["name"],
            "system_prompt": agent_def.get("system_prompt", ""),
            "output_mode": agent_def.get("output_mode", "capsule"),
            "requires_voice": voice,
            "capture_selection": agent_def.get("selection", False),
            "capture_window": voice,
            "capture_screenshot": "auto" if voice else "never",
            "llm_tier": "thinking",
            "orchestrable": True,
            "mcps": [mcp_id] if mcp_id else [],
            # Champs securite propages depuis le groupe
            "write_mode": group_write_mode,
            "blocked_tools": group_blocked_tools,
            "max_writes": group_max_writes,
            # Champs optionnels de l'agent inline
            "web_search_enabled": agent_def.get("web_search", False),
            "memory_enabled": agent_def.get("memory", False),
        }
        agent_result = await _call_api("POST", "/api/agents", agent_data)
        if agent_result.get("ok"):
            created_agents.append(agent_result["agent"]["id"])
        else:
            failed_agents.append({"name": agent_def["name"], "error": agent_result.get("error", "Unknown")})

    # 4. Cabler le scope
    if created_agents and group_id:
        await _call_api("PUT", f"/api/agents/{group_id}", {"orchestrator_scope": created_agents})

    response = {
        "ok": True,
        "group_id": group_id,
        "agents_created": created_agents,
        "message": f"Groupe '{args['name']}' cree avec {len(created_agents)} agents",
    }
    if reused_agents:
        response["reused"] = reused_agents
        response["message"] += f" ({len(reused_agents)} reutilises: {', '.join(reused_agents)})"
    if failed_agents:
        response["warnings"] = failed_agents
        response["message"] += f" ({len(failed_agents)} echecs)"
    return _json_result(response)


async def _update_agent(args: dict) -> list[TextContent]:
    agent_id = args.get("agent_id")
    data = {k: v for k, v in args.items() if k != "agent_id"}
    return _json_result(await _call_api("PUT", f"/api/agents/{agent_id}", data))


async def _delete_agent(agent_id: str) -> list[TextContent]:
    return _json_result(await _call_api("DELETE", f"/api/agents/{agent_id}"))


# ============================================================================
# Groupes
# ============================================================================


async def _add_to_group_impl(group_id: str, agent_id: str) -> dict:
    """Helper: ajoute un agent au scope d'un groupe."""
    group = await _call_api("GET", f"/api/agents/{group_id}")
    if not group.get("ok"):
        return group
    scope = group.get("agent", {}).get("orchestrator_scope", [])
    if agent_id not in scope:
        scope.append(agent_id)
        return await _call_api("PUT", f"/api/agents/{group_id}", {"orchestrator_scope": scope})
    return {"ok": True, "message": "Deja dans le groupe"}


async def _remove_from_group_impl(group_id: str, agent_id: str) -> dict:
    """Helper: retire un agent du scope d'un groupe."""
    group = await _call_api("GET", f"/api/agents/{group_id}")
    if not group.get("ok"):
        return group
    scope = group.get("agent", {}).get("orchestrator_scope", [])
    if agent_id in scope:
        scope.remove(agent_id)
        return await _call_api("PUT", f"/api/agents/{group_id}", {"orchestrator_scope": scope})
    return {"ok": True, "message": "Agent non present dans le groupe"}


async def _list_groups() -> list[TextContent]:
    result = await _call_api("GET", "/api/agents?include_archived=false")
    # API returns bare list — normalize
    if isinstance(result, list):
        agents = result
    elif isinstance(result, dict) and result.get("ok"):
        agents = result.get("agents", [])
    else:
        return _json_result(result)
    groups = [a for a in agents if a.get("is_orchestrator")]
    return _json_result({"ok": True, "groups": groups, "count": len(groups)})


async def _add_to_group(group_id: str, agent_id: str) -> list[TextContent]:
    return _json_result(await _add_to_group_impl(group_id, agent_id))


async def _remove_from_group(group_id: str, agent_id: str) -> list[TextContent]:
    return _json_result(await _remove_from_group_impl(group_id, agent_id))


# ============================================================================
# Connecteurs (MCPs)
# ============================================================================


async def _list_mcps() -> list[TextContent]:
    result = await _call_api("GET", "/api/mcps/list")
    if isinstance(result, list):
        return _json_result({"ok": True, "mcps": result, "count": len(result)})
    return _json_result(result)


async def _browse_mcp_library(category: str | None = None, search: str | None = None) -> list[TextContent]:
    result = await _call_api("GET", "/api/mcps/library")
    if not isinstance(result, dict) or not result.get("library"):
        return _json_result(result)
    library = result.get("library", {})

    if category:
        library = {
            k: v for k, v in library.items() if v.get("category") == category or category in v.get("categories", [])
        }

    if search:
        q = search.lower()
        library = {
            k: v
            for k, v in library.items()
            if q in k.lower() or q in v.get("name", "").lower() or q in v.get("description", "").lower()
        }

    return _json_result({"ok": True, "library": library, "count": len(library)})


async def _install_mcp(mcp_id: str) -> list[TextContent]:
    """Installe un MCP et retourne les champs de config requis pour guider le LLM."""
    result = await _call_api("POST", f"/api/mcps/{mcp_id}/install")
    if not result.get("ok"):
        return _json_result(result)

    # Enrichir avec le schema de configuration
    config = await _call_api("GET", f"/api/mcps/{mcp_id}/config-schema")
    enriched = dict(result)

    if isinstance(config, dict) and config.get("ok"):
        schema = config.get("schema", {})
        fields = schema.get("fields", []) if isinstance(schema, dict) else []
        auth_type = config.get("mcp", {}).get("auth_type", "none")

        requires_config = bool(fields)
        enriched["requires_config"] = requires_config
        enriched["auth_type"] = auth_type
        enriched["config_fields"] = [
            {"id": f.get("id"), "type": f.get("type"), "label": f.get("label", f.get("id")), "hint": f.get("hint", "")}
            for f in fields
        ]

        if auth_type == "oauth":
            enriched["next_step"] = f"Appelez start_mcp_auth(mcp_id='{mcp_id}') pour lancer l'authentification."
        elif requires_config:
            enriched["next_step"] = "Demandez les valeurs a l'utilisateur puis appelez configure_mcp."
        else:
            enriched["next_step"] = "Le connecteur est pret a l'emploi."

    return _json_result(enriched)


async def _add_mcp(args: dict) -> list[TextContent]:
    data = {
        "name": args.get("name"),
        "command": args.get("command"),
        "args": args.get("args", []),
        "env": args.get("env", {}),
        "description": args.get("description", ""),
    }
    return _json_result(await _call_api("POST", "/api/mcps/custom", data))


# ============================================================================
# Introspection
# ============================================================================


async def _get_last_result(n: int = 1) -> list[TextContent]:
    n = max(1, min(n, 10))
    result = await _call_api("GET", "/api/history")
    if isinstance(result, list) and len(result) > 0:
        return _json_result(result[:n])
    return _json_result({"message": "Aucune action recente dans l'historique."})


async def _reload_config() -> list[TextContent]:
    """Force le rechargement des agents et connecteurs."""
    import time

    signal_path = Path(__file__).parent.parent / "data" / ".mcp_refresh_signal"
    try:
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.write_text(str(time.time()))
    except Exception:
        pass

    result = await _call_api("GET", "/api/agents")
    count = len(result) if isinstance(result, list) else 0
    return _json_result({"ok": True, "message": f"Config rechargee. {count} agents trouves."})


async def _batch_delete(agent_ids: list[str]) -> list[TextContent]:
    """Supprime plusieurs agents en un appel."""
    if len(agent_ids) > 20:
        return _json_result({"ok": False, "error": "Maximum 20 agents par batch"})

    deleted = []
    failed = []
    for agent_id in agent_ids:
        result = await _call_api("DELETE", f"/api/agents/{agent_id}")
        if isinstance(result, dict) and result.get("ok"):
            deleted.append(agent_id)
        else:
            error = result.get("error", "Unknown") if isinstance(result, dict) else "Unknown"
            failed.append({"id": agent_id, "error": error})

    return _json_result(
        {
            "ok": len(failed) == 0,
            "deleted": deleted,
            "failed": failed,
            "message": f"{len(deleted)} supprimes, {len(failed)} echecs",
        }
    )


# ============================================================================
# Server Instructions (injectees dans chaque conversation MCP)
# ============================================================================

_INSTRUCTIONS = """\
# Vicsia Studio — Guide de configuration

## Qu'est-ce que Vicsia

Vicsia est un assistant vocal. L'utilisateur appuie sur un raccourci, parle, et relache.

Agents standalone (raccourci dedie) : ce que l'utilisateur dit est le CONTENU (dicter, corriger,
traduire). L'agent fait toujours la meme chose. Input vocal = matiere a traiter.
Exemples : transcription, correction, traduction.

Groupes (orchestrateur + agents enfants) : ce que l'utilisateur dit est une INSTRUCTION (un
ordre). Le routeur choisit l'agent enfant selon ce qui a ete dit.
Exemples : "lis mes mails", "cherche des vols pour Lisbonne", "ecris un brouillon pour Jean".

Regle : un agent va dans un groupe seulement si l'instruction vocale change son comportement.
Si l'agent fait toujours la meme chose -> raccourci standalone.
Si le comportement depend de ce que l'utilisateur dit -> groupe.


## Creation simplifiee

create_agent : seul "name" est requis. Auto-derive automatiquement :
- hotkey : assigne depuis le pool disponible (ne pas specifier)
- description : generee depuis le system_prompt par LLM
- routing_keywords : extraits depuis le nom/description par LLM
- llm_tier : "thinking" par defaut (LLM actif)

voice=true (defaut) : requires_voice=true, capture_window=true, capture_screenshot=auto
voice=false : requires_voice=false, capture_window=false, capture_screenshot=never
memory=true : active la memoire persistante de l'agent
web_search=true : active la recherche web native Mistral

Note : output_mode par defaut = "capsule". Specifier "paste" pour les agents texte (dictee,
correction, ghostwriter). tts_enabled=true ajoute la lecture vocale (capsule uniquement).

create_group : cree l'orchestrateur et les agents inline en un seul appel.
- Le MCP est herite automatiquement par les agents enfants
- orchestrable=true est auto-set sur tous les agents inline (ne pas specifier)
- output_mode par defaut = "capsule" pour l'orchestrateur ET tous les enfants
- Les champs securite (write_mode, blocked_tools, max_writes) se definissent sur le groupe
  et sont propages automatiquement aux enfants


## Regles absolues

1. JAMAIS inventer une cle API, un token ou un secret. Toujours demander a l'utilisateur.
2. JAMAIS afficher une cle API en clair. Confirmer avec "Cle configuree" seulement.
3. Toujours appeler get_mcp_config avant configure_mcp pour connaitre les champs exacts.
4. Toujours verifier le catalogue (browse_mcp_library) avant install_mcp pour confirmer l'ID.
5. Pour OAuth Microsoft : polling poll_mcp_auth toutes les 5s, max 36 iterations (3 min).
6. Pour OAuth Google : pas de polling. L'auth se fait automatiquement au premier appel d'agent.
7. Toujours remplir descriptions (agents et groupes) — elles sont la base du routage vocal.
8. Toujours verifier list_agents(summary=true) avant de creer — eviter les doublons.


## Routage vocal — descriptions

La description du connecteur est le signal le plus fort pour choisir le bon groupe.
La description de chaque agent permet de choisir le bon agent dans le groupe.

Bonne description d'agent : "Lire, rechercher et resumer vos emails" (actions concretes)
Mauvaise description : "Agent email" (trop vague, pas d'action)

Bonne description de connecteur : "Lecture et envoi d'emails via Gmail"
Mauvaise description de connecteur : "Gmail" (pas d'actions)


## Ecrire un bon system_prompt

Agents standalone (raccourci dedie, input = CONTENU) :
Prompt court (3-5 lignes), rigide, finit par "Reponds UNIQUEMENT par X".
Pas de workflow, pas de reference aux outils MCP. output_mode="paste".

Exemple — Correction :
  Corrige le texte selectionne: orthographe, grammaire, ponctuation.
  Garde le sens exact. Reponds UNIQUEMENT par le texte corrige, sans ajout.

Agents dans un groupe (input = INSTRUCTION) :
Workflow numerote, "Adapte-toi au connecteur disponible",
contraintes de securite explicites ("JAMAIS envoyer", "brouillons uniquement"). output_mode="capsule".

Exemple — Lecteur Email :
  Tu es un assistant email specialise en LECTURE et SYNTHESE.
  Adapte-toi au connecteur email disponible (Gmail ou Outlook).
  WORKFLOW: 1. Recherche les emails  2. Lis le contenu complet  3. Redige une synthese
  Utilise UNIQUEMENT les outils listes dans OUTILS MCP DISPONIBLES.
  Tu ne peux PAS envoyer, modifier ou supprimer des emails.


## Securite — champs du groupe

Ces champs se definissent sur create_group et sont propages automatiquement aux agents enfants.
Ils sont aussi disponibles dans update_agent pour une edition avancee.

blocked_tools : patterns de tools jamais exposes au LLM. Ex : ["send_*", "delete_*", "trash_*"]
write_mode : "none" (lecture seule), "ask" (confirmation requise), "auto" (automatique)
max_writes : nombre max d'ecritures par execution, 1 a 6 (defaut : 1)

Agent lecteur : write_mode="none", blocked_tools=["send_*", "delete_*", "create_*"]
Agent redacteur : write_mode="auto", max_writes=1, blocked_tools=["send_*", "delete_*"]

Il n'y a PAS de champ "read_only" dans le schema — utiliser write_mode="none" a la place.


## Output modes

Agents texte standalone (dictee, correction) : output_mode="paste" (insere dans l'app active)
Tous les autres agents (groupe, question, MCP) : output_mode="capsule" (mini-capsule Vicsia, defaut)
Lecture vocale optionnelle : tts_enabled=true (s'ajoute a la capsule, pas compatible avec paste)

create_group -> output_mode="capsule" par defaut pour tout le groupe.
create_agent -> output_mode="capsule" par defaut. Specifier "paste" pour agents texte.


## Pack de base (eviter les doublons)

Au premier lancement, Vicsia installe automatiquement un pack de base :
- Transcription (ctrl+space) : micro vers texte brut, llm_tier="none", output_mode="paste"
- Transcription corrigee (ctrl+shift+space) : micro vers texte corrige, output_mode="paste"
- Correction (ctrl+<) : selection vers texte corrige, requires_voice=false, output_mode="paste"
- Traduction vers FR (ctrl+shift+t) : requires_voice=false, output_mode="paste"
- Groupe Vocal (ctrl+g) : orchestrateur avec agents Assistant (capture_screenshot=auto)
  et Recherche Web (web_search_enabled=true)

Connecteurs actifs par defaut : memory, filesystem.
Toujours verifier list_agents(summary=true) avant de creer — ne pas dupliquer ces agents.


## Roles

Configurer : installer des connecteurs, creer des groupes d'agents, configurer les parametres.
Conseiller : identifier les logiciels ou Vicsia apporterait de la valeur (emails, CRM, billing).
Orienter : pour les logiciels sans connecteur dans le catalogue, donner : contact@vicsia.fr


## Auth OAuth — rappel

Microsoft (outlook-graph) :
  configure_mcp(mcp_id, {access_mode: "readonly"}) -> start_mcp_auth ->
  polling poll_mcp_auth toutes les 5s -> afficher device_code + URL microsoft.com/devicelogin
  -> attendre success / expired / declined

Google (google-workspace) :
  configurer client_id + client_secret -> start_mcp_auth -> toggle_mcp ->
  l'auth se fait automatiquement au premier appel d'un agent
"""


# ============================================================================
# MCP Server
# ============================================================================


def main():
    """Point d'entree du serveur MCP vicsia-studio (admin/construction)."""
    server = Server("vicsia-studio")

    @server.list_tools()
    async def list_tools():
        return get_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        return await handle_tool(name, arguments)

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            init_options = InitializationOptions(
                server_name="vicsia-studio",
                server_version="2.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
                instructions=_INSTRUCTIONS,
            )
            await server.run(read_stream, write_stream, init_options)

    asyncio.run(run())


if __name__ == "__main__":
    main()
