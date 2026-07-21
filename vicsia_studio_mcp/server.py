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

OUTILS (50):
- Agents      : create_agent, create_group, update_agent, delete_agent, list_agents, get_agent
- Lifecycle   : archive_agent, restore_agent, get_available_hotkeys
- Groupes     : list_groups, add_to_group, remove_from_group, list_packages
- Connecteurs : list_mcps, get_mcp, toggle_mcp, add_mcp, delete_mcp,
                browse_mcp_library, install_mcp, get_mcp_config, configure_mcp,
                start_mcp_auth, poll_mcp_auth
- MCP tools   : list_mcp_tools, test_mcp, get_mcp_suggested_agents, reinstall_mcp
- Projets     : list_projects, create_project, get_project, update_project, delete_project
- Scripts     : upload_script, validate_script, list_connector_scripts,
                list_project_secrets, set_project_secret, delete_project_secret
- Automations : list_automations, create_automation, get_automation, update_automation,
                delete_automation, run_automation_now, get_automation_runs,
                get_automation_webhook_url
- Settings    : get_settings, update_settings
- Introspection: get_vicsia_status

Projets (Chat IA / mini-app) : un Projet porte un prompt + connecteurs MCP + un script Python
(secrets PEP 723). Il peut etre expose comme connecteur (available_as_connector) puis branche
sur un agent/groupe via update_agent(script_connectors=[project_id]) ou create_group(script_connectors).
Automations : declencheurs (interval/daily/webhook) qui lancent un Projet en tache de fond.

Pour usage vocal (capacite native Free), seul un sous-set est expose via le
`tool_sets` "voice" defini cote MCPConfig : create_agent, create_group, list_groups.
Les autres outils restent disponibles pour les consommateurs externes (Claude Desktop, etc.).
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


def _json_result(result, compact: bool = False) -> list[TextContent]:
    """Encode le resultat en JSON pour le retour MCP.

    compact=True : pas d'indent — economise des tokens pour les listes longues
    (list_agents en mode summary par ex.). Defaut : indent=2 pour lisibilite.
    """
    if compact:
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(result, ensure_ascii=False, indent=2)
    return [TextContent(type="text", text=text)]


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
                "Liste tous les agents Vicsia en format compact par defaut (id, name, hotkey, "
                "is_group, parent_group, members pour les groupes). Utile pour eviter les doublons "
                "avant create_agent. Passez full=true pour avoir les details complets."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_archived": {"type": "boolean", "description": "Inclure les archives (defaut: false)"},
                    "full": {
                        "type": "boolean",
                        "description": "Retourne tous les champs (system_prompt, description, mcps, etc.) au lieu du format compact. Defaut: false.",
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
                    "script_connectors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "IDs de Projets (available_as_connector=true) branches comme connecteurs-script "
                            "sur l'orchestrateur du groupe. Lister via list_connector_scripts."
                        ),
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
                                    "description": "Mode de sortie (defaut: capsule). paste pour agents texte.",
                                },
                                "tts_enabled": {
                                    "type": "boolean",
                                    "description": "Lecture vocale en plus de la capsule (defaut: false). Incompatible avec paste.",
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
                    "confirm_detach": {
                        "type": "boolean",
                        "description": "Confirme la personnalisation du prompt d'un agent bibliotheque lie (detache l'agent des mises a jour automatiques). Requis uniquement si le premier appel renvoie une erreur de detachement.",
                    },
                    "script_connectors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "IDs de Projets (available_as_connector=true) branches comme connecteurs-script "
                            "sur cet agent/groupe. L'agent peut alors appeler le script comme un outil. "
                            "Lister les projets disponibles via list_connector_scripts."
                        ),
                    },
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
            description=(
                "Ajoute un connecteur custom (commande + args + env). "
                "command doit etre dans la whitelist (npx, uvx, python, python3, node, uv, dotnet) — "
                "PAS de chemin absolu d'interpreteur. env ne peut pas contenir PATH/HOME/PYTHONPATH (bloques). "
                "MCP LOCAL (package Python livre avec un repo) : 'python -m mon_pkg' ne se resout que si Vicsia "
                "tourne avec cwd=repo (KO en app packagee). Contournement fiable et sandbox-safe : "
                'command="uv", args=["run", "--directory", "<chemin_repo>", "--no-sync", "python", "-m", "<pkg>"].'
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "command": {
                        "type": "string",
                        "description": "Commande whitelistee (npx, uvx, python, python3, node, uv, dotnet)",
                    },
                    "args": {"type": "array", "items": {"type": "string"}},
                    "env": {"type": "object", "description": "Variables d'env (PATH/HOME/PYTHONPATH interdits)"},
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
        # === Introspection connecteurs ===
        Tool(
            name="list_mcp_tools",
            description=(
                "Liste les tool-sets (groupes d'outils) d'un connecteur avec le detail des outils exposes "
                "(tool_count, read_count, write_count). Utile pour verifier ce qu'un MCP fournit avant de "
                "l'assigner a un agent, sans avoir a le declencher a la voix."
            ),
            inputSchema={"type": "object", "properties": {"mcp_id": {"type": "string"}}, "required": ["mcp_id"]},
        ),
        Tool(
            name="test_mcp",
            description=(
                "Verifie qu'un connecteur est installe, actif et expose des outils. Retourne son statut "
                "(enabled), le nombre d'outils detectes et un diagnostic. Un connecteur sans tool-sets ou "
                "desactive est signale comme non pret."
            ),
            inputSchema={"type": "object", "properties": {"mcp_id": {"type": "string"}}, "required": ["mcp_id"]},
        ),
        Tool(
            name="get_mcp_suggested_agents",
            description="Propose des definitions d'agents pretes a l'emploi pour un connecteur (un agent par tool-set, avec securite adaptee).",
            inputSchema={"type": "object", "properties": {"mcp_id": {"type": "string"}}, "required": ["mcp_id"]},
        ),
        Tool(
            name="reinstall_mcp",
            description="Force le re-telechargement d'un connecteur du catalogue au prochain usage (recalcule args/version depuis la librairie).",
            inputSchema={"type": "object", "properties": {"mcp_id": {"type": "string"}}, "required": ["mcp_id"]},
        ),
        # === Projets (Chat IA / mini-app) ===
        Tool(
            name="list_projects",
            description=(
                "Liste les Projets (Chat IA / mini-app). Un Projet = prompt + connecteurs MCP + script Python. "
                "launcher_only=true ne retourne que les projets visibles dans le lanceur (mini_app_enabled)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "launcher_only": {"type": "boolean", "description": "Filtrer sur mini_app_enabled (defaut: false)"}
                },
            },
        ),
        Tool(
            name="create_project",
            description="Cree un Projet (Chat IA / mini-app). Seul le nom est requis. Ensuite: upload_script, update_project pour prompt/connecteurs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nom du projet (max 200 chars)"},
                    "description": {"type": "string", "description": "Description courte (max 500 chars)"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="get_project",
            description="Details d'un projet: config, presence d'un script, historique de runs, securite, metadata PEP 723, explication.",
            inputSchema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"],
            },
        ),
        Tool(
            name="update_project",
            description=(
                "Met a jour un projet. Seuls les champs fournis sont modifies. "
                "available_as_connector=true expose le script comme connecteur branchable sur un agent/groupe "
                "(via update_agent script_connectors). mini_app_enabled=false le cache du lanceur."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "prompt": {"type": "string", "description": "Instructions injectees dans le chat du projet"},
                    "connectors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IDs de MCPs prioritaires",
                    },
                    "artifact_template": {"type": "string"},
                    "artifact_template_id": {"type": "string"},
                    "model": {
                        "type": "string",
                        "enum": ["small", "medium"],
                        "description": "Tier LLM du projet (omettre pour heriter du global)",
                    },
                    "routing_keywords": {"type": "array", "items": {"type": "string"}},
                    "available_as_connector": {
                        "type": "boolean",
                        "description": "Expose le script comme connecteur branchable sur un agent/groupe",
                    },
                    "mini_app_enabled": {"type": "boolean", "description": "Visible dans le lanceur de Projets"},
                },
                "required": ["project_id"],
            },
        ),
        Tool(
            name="delete_project",
            description="Supprime (soft-delete) un projet",
            inputSchema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"],
            },
        ),
        # === Scripts, secrets, connecteurs-script ===
        Tool(
            name="upload_script",
            description=(
                "Televerse le code Python d'un projet (max 1 Mo). Le script passe une analyse de securite "
                "(AST + LLM). Si bloque, le retour contient security.blocked=true — reappeler avec "
                "acknowledge_risk=true pour forcer (le script reste sandboxe). Retourne security, metadata "
                "(PEP 723: secrets declares, dependances), explanation et capabilities."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "source": {"type": "string", "description": "Code source Python complet"},
                    "acknowledge_risk": {
                        "type": "boolean",
                        "description": "Force l'upload malgre un blocage securite (defaut: false)",
                    },
                },
                "required": ["project_id", "source"],
            },
        ),
        Tool(
            name="validate_script",
            description="Valide un script Python (securite AST+LLM, metadata PEP 723, capabilities) SANS le persister. Utile avant upload_script.",
            inputSchema={
                "type": "object",
                "properties": {"source": {"type": "string", "description": "Code source Python a valider"}},
                "required": ["source"],
            },
        ),
        Tool(
            name="list_connector_scripts",
            description=(
                "Liste les Projets exposes comme connecteurs (available_as_connector=true), avec leur id/nom/description. "
                "Ces ids sont a passer dans update_agent(script_connectors=[...]) ou create_group(script_connectors=[...])."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_project_secrets",
            description="Liste les secrets declares (PEP 723) d'un projet: id, label, env_var, configured (bool), valeur masquee.",
            inputSchema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"],
            },
        ),
        Tool(
            name="set_project_secret",
            description=(
                "Definit la valeur d'un secret DECLARE d'un projet (PEP 723). L'id doit exister dans les secrets "
                "declares du script (voir list_project_secrets). JAMAIS inventer une valeur — demander a l'utilisateur."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "secret_id": {"type": "string", "description": "ID du secret declare (PEP 723)"},
                    "value": {"type": "string", "description": "Valeur du secret (max 4096 chars)"},
                },
                "required": ["project_id", "secret_id", "value"],
            },
        ),
        Tool(
            name="delete_project_secret",
            description="Supprime la valeur d'un secret d'un projet.",
            inputSchema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}, "secret_id": {"type": "string"}},
                "required": ["project_id", "secret_id"],
            },
        ),
        # === Automations (declencheurs) ===
        Tool(
            name="list_automations",
            description="Liste les automations (declencheurs qui lancent un Projet: interval/daily/webhook).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="create_automation",
            description=(
                "Cree une automation qui lance un Projet selon un declencheur. "
                "trigger: {type:'interval', every_minutes:N} | {type:'daily', at:'HH:MM'} | {type:'webhook'}. "
                "hosted=true (Pro x3) execute cote cloud h24 machine eteinte."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nom de l'automation (max 200 chars)"},
                    "project_id": {"type": "string", "description": "ID du Projet a executer"},
                    "inputs": {"type": "object", "description": "Valeurs d'entree du script {field_id: value}"},
                    "trigger": {
                        "type": "object",
                        "description": "interval/daily/webhook — ex: {type:'interval', every_minutes:60}",
                    },
                    "enabled": {"type": "boolean", "description": "Activer immediatement (defaut: false)"},
                    "hosted": {"type": "boolean", "description": "Execution cloud h24 (Pro x3 requis, defaut: false)"},
                    "webhook_secret": {"type": "string", "description": "Secret HMAC pour trigger webhook (optionnel)"},
                },
                "required": ["name", "project_id", "inputs", "trigger"],
            },
        ),
        Tool(
            name="get_automation",
            description="Details d'une automation par son ID.",
            inputSchema={
                "type": "object",
                "properties": {"automation_id": {"type": "string"}},
                "required": ["automation_id"],
            },
        ),
        Tool(
            name="update_automation",
            description="Met a jour une automation (partiel). Champs: name, inputs, trigger, enabled, hosted, webhook_secret.",
            inputSchema={
                "type": "object",
                "properties": {
                    "automation_id": {"type": "string"},
                    "name": {"type": "string"},
                    "inputs": {"type": "object"},
                    "trigger": {"type": "object"},
                    "enabled": {"type": "boolean"},
                    "hosted": {"type": "boolean"},
                    "webhook_secret": {"type": "string"},
                },
                "required": ["automation_id"],
            },
        ),
        Tool(
            name="delete_automation",
            description="Supprime une automation.",
            inputSchema={
                "type": "object",
                "properties": {"automation_id": {"type": "string"}},
                "required": ["automation_id"],
            },
        ),
        Tool(
            name="run_automation_now",
            description="Declenche immediatement une automation (execution manuelle) et retourne le run.",
            inputSchema={
                "type": "object",
                "properties": {"automation_id": {"type": "string"}},
                "required": ["automation_id"],
            },
        ),
        Tool(
            name="get_automation_runs",
            description="Historique des executions (runs) d'une automation.",
            inputSchema={
                "type": "object",
                "properties": {"automation_id": {"type": "string"}},
                "required": ["automation_id"],
            },
        ),
        Tool(
            name="get_automation_webhook_url",
            description="Retourne l'URL webhook d'une automation de type webhook (locale ou hebergee).",
            inputSchema={
                "type": "object",
                "properties": {"automation_id": {"type": "string"}},
                "required": ["automation_id"],
            },
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
    ]


# ============================================================================
# Tool dispatch
# ============================================================================


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute un outil et retourne le resultat."""

    handlers = {
        # Agents
        "list_agents": lambda: _list_agents(arguments.get("include_archived", False), arguments.get("full", False)),
        "get_agent": lambda: _get_agent(arguments["agent_id"]),
        "create_agent": lambda: _create_agent(arguments),
        "create_group": lambda: _create_group(arguments),
        "update_agent": lambda: _update_agent(arguments),
        "delete_agent": lambda: _delete_agent(arguments["agent_id"]),
        # Lifecycle
        "archive_agent": lambda: _api_post(f"/api/agents/{arguments['agent_id']}/archive"),
        "restore_agent": lambda: _api_post(f"/api/agents/{arguments['agent_id']}/restore"),
        "get_available_hotkeys": lambda: _api_get("/api/agents/hotkeys"),
        # Groupes
        "list_groups": lambda: _list_groups(),
        "add_to_group": lambda: _add_to_group(arguments["group_id"], arguments["agent_id"]),
        "remove_from_group": lambda: _remove_from_group(arguments["group_id"], arguments["agent_id"]),
        "list_packages": lambda: _api_get("/api/agents/library"),
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
        # Introspection connecteurs
        "list_mcp_tools": lambda: _api_get(f"/api/mcps/{arguments['mcp_id']}/tool-sets"),
        "test_mcp": lambda: _test_mcp(arguments["mcp_id"]),
        "get_mcp_suggested_agents": lambda: _api_get(f"/api/mcps/{arguments['mcp_id']}/suggested-agents"),
        "reinstall_mcp": lambda: _api_post(f"/api/mcps/{arguments['mcp_id']}/reinstall"),
        # Projets
        "list_projects": lambda: _list_projects(arguments.get("launcher_only", False)),
        "create_project": lambda: _api_post(
            "/api/miniapp/projects",
            {"name": arguments["name"], "description": arguments.get("description", "")},
        ),
        "get_project": lambda: _api_get(f"/api/miniapp/projects/{arguments['project_id']}"),
        "update_project": lambda: _api_patch(
            f"/api/miniapp/projects/{arguments['project_id']}",
            {k: v for k, v in arguments.items() if k != "project_id"},
        ),
        "delete_project": lambda: _api_delete(f"/api/miniapp/projects/{arguments['project_id']}"),
        # Scripts, secrets, connecteurs-script
        "upload_script": lambda: _api_post(
            f"/api/miniapp/projects/{arguments['project_id']}/script",
            {"source": arguments["source"], "acknowledge_risk": arguments.get("acknowledge_risk", False)},
        ),
        "validate_script": lambda: _api_post("/api/miniapp/validate-script", {"source": arguments["source"]}),
        "list_connector_scripts": lambda: _api_get("/api/miniapp/connector-scripts"),
        "list_project_secrets": lambda: _api_get(f"/api/miniapp/projects/{arguments['project_id']}/secrets"),
        "set_project_secret": lambda: _api_post(
            f"/api/miniapp/projects/{arguments['project_id']}/secrets",
            {"id": arguments["secret_id"], "value": arguments["value"]},
        ),
        "delete_project_secret": lambda: _api_delete(
            f"/api/miniapp/projects/{arguments['project_id']}/secrets/{arguments['secret_id']}"
        ),
        # Automations
        "list_automations": lambda: _api_get("/api/automation/automations"),
        "create_automation": lambda: _api_post("/api/automation/automations", _automation_create_payload(arguments)),
        "get_automation": lambda: _api_get(f"/api/automation/automations/{arguments['automation_id']}"),
        "update_automation": lambda: _api_patch(
            f"/api/automation/automations/{arguments['automation_id']}",
            {k: v for k, v in arguments.items() if k != "automation_id"},
        ),
        "delete_automation": lambda: _api_delete(f"/api/automation/automations/{arguments['automation_id']}"),
        "run_automation_now": lambda: _api_post(f"/api/automation/automations/{arguments['automation_id']}/run-now"),
        "get_automation_runs": lambda: _api_get(f"/api/automation/automations/{arguments['automation_id']}/runs"),
        "get_automation_webhook_url": lambda: _api_get(f"/api/automation/webhook-url/{arguments['automation_id']}"),
        # Settings
        "get_settings": lambda: _api_get("/api/settings"),
        "update_settings": lambda: _api_put("/api/settings", arguments),
        # Introspection
        "get_vicsia_status": lambda: _api_get("/api/status"),
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


async def _api_patch(path: str, data: dict | None = None) -> list[TextContent]:
    return _json_result(await _call_api("PATCH", path, data))


# ============================================================================
# Agents
# ============================================================================


async def _list_agents(include_archived: bool = False, full: bool = False) -> list[TextContent]:
    """Liste les agents — format COMPACT par defaut pour economiser des tokens.

    Format compact (defaut) : id, name, hotkey, is_group (true si orchestrateur),
    parent_group (nom du groupe parent ou null), members (liste des noms si is_group).
    Format full=true : tous les champs (system_prompt, mcps, description...).
    """
    param = "true" if include_archived else "false"
    result = await _call_api("GET", f"/api/agents?include_archived={param}")
    if isinstance(result, list):
        agents = result
    elif isinstance(result, dict) and "error" in result:
        return _json_result(result)
    else:
        agents = result.get("agents", [])

    if full:
        return _json_result({"ok": True, "agents": agents, "count": len(agents)})

    # Format compact : id + name + hotkey + structure groupe
    # Etape 1 : map id -> name pour resoudre les scopes
    id_to_name = {a.get("id"): a.get("name", "?") for a in agents}
    # Etape 2 : pour chaque agent, calculer parent_group via les orchestrator_scope
    parent_of: dict[str, str] = {}
    for a in agents:
        if a.get("is_orchestrator"):
            for child_id in a.get("orchestrator_scope", []) or []:
                if child_id in id_to_name:
                    parent_of[child_id] = a.get("name", "?")

    compact = []
    for a in agents:
        item: dict = {
            "id": a.get("id"),
            "name": a.get("name"),
        }
        if a.get("hotkey"):
            item["hotkey"] = a["hotkey"]
        if a.get("is_orchestrator"):
            item["is_group"] = True
            members = [id_to_name.get(cid) for cid in (a.get("orchestrator_scope") or []) if cid in id_to_name]
            if members:
                item["members"] = members
        elif a.get("id") in parent_of:
            item["parent_group"] = parent_of[a["id"]]
        if a.get("archived"):
            item["archived"] = True
        compact.append(item)

    return _json_result({"ok": True, "agents": compact, "count": len(compact)}, compact=True)


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
    group_script_connectors = args.get("script_connectors", [])

    # 1. Creer l'orchestrateur
    # hotkey="" : un groupe est atteint par le routeur global, pas par un raccourci dedie
    # (ne pas consommer le pool de raccourcis — friction #5 de l'audit).
    group_data = {
        "name": args["name"],
        "description": args.get("description", ""),
        "system_prompt": "",
        "output_mode": "capsule",
        "hotkey": "",
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
        "script_connectors": group_script_connectors,
    }
    group_result = await _call_api("POST", "/api/agents", group_data)
    if not group_result.get("ok"):
        return _json_result(group_result)

    group_id = group_result.get("agent", {}).get("id")
    created_agents = []
    reused_agents = []
    failed_agents = []
    warnings: list[dict] = []

    # 2. Index des agents existants pour eviter les doublons.
    #    On garde l'objet complet pour verifier la COMPATIBILITE avant de reutiliser
    #    (bug #1 : un homonyme incompatible — ex. agent de selection sans voix — ne doit
    #    PAS remplacer silencieusement un agent orchestrable de groupe).
    existing = await _call_api("GET", "/api/agents?include_archived=false")
    name_to_agent: dict[str, dict] = {}
    if isinstance(existing, list):
        name_to_agent = {a["name"].lower(): a for a in existing if a.get("name")}

    # 3. Creer les agents enfants (heritent du connecteur)
    for agent_def in args.get("agents", []):
        voice = agent_def.get("voice", True)

        # Reutiliser un agent existant homonyme UNIQUEMENT s'il est compatible :
        # meme requires_voice et orchestrable (routable dans un groupe).
        existing_agent = name_to_agent.get(agent_def["name"].lower())
        if existing_agent:
            compatible = (
                existing_agent.get("requires_voice", True) == voice
                and existing_agent.get("orchestrable", False)
                and not existing_agent.get("is_orchestrator", False)
            )
            if compatible:
                created_agents.append(existing_agent["id"])
                reused_agents.append(agent_def["name"])
                continue
            # Homonyme incompatible : on cree un agent neuf et on avertit bruyamment.
            warnings.append(
                {
                    "name": agent_def["name"],
                    "warning": (
                        f"Un agent existant nomme '{agent_def['name']}' est incompatible "
                        f"(requires_voice={existing_agent.get('requires_voice')}, "
                        f"orchestrable={existing_agent.get('orchestrable', False)}) — "
                        "un nouvel agent a ete cree au lieu de le reutiliser."
                    ),
                }
            )

        agent_data = {
            "name": agent_def["name"],
            "system_prompt": agent_def.get("system_prompt", ""),
            "output_mode": agent_def.get("output_mode", "capsule"),
            "tts_enabled": agent_def.get("tts_enabled", False),
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
    all_warnings = warnings + failed_agents
    if all_warnings:
        response["warnings"] = all_warnings
    if failed_agents:
        response["message"] += f" ({len(failed_agents)} echecs)"
    if warnings:
        response["message"] += f" ({len(warnings)} homonymes incompatibles recrees)"
    return _json_result(response)


async def _update_agent(args: dict) -> list[TextContent]:
    agent_id = args.get("agent_id")
    data = {k: v for k, v in args.items() if k != "agent_id"}
    result = await _call_api("PUT", f"/api/agents/{agent_id}", data)
    if isinstance(result, dict) and not result.get("ok") and result.get("detach_required"):
        result = {
            **result,
            "error": (
                "Cet agent est lie a la bibliotheque. Repasse l'appel avec confirm_detach=true "
                "pour le personnaliser (il sera detache des mises a jour automatiques)."
            ),
        }
    return _json_result(result)


async def _delete_agent(agent_id: str) -> list[TextContent]:
    return _json_result(await _call_api("DELETE", f"/api/agents/{agent_id}"))


# ============================================================================
# Groupes
# ============================================================================


async def _add_to_group_impl(group_id: str, agent_id: str) -> dict:
    """Helper: ajoute un agent au scope d'un groupe et lui fait heriter des reglages du groupe.

    A l'ajout, l'agent herite des champs securite (write_mode, blocked_tools, max_writes),
    devient orchestrable et recupere les connecteurs du groupe (union) — sinon il arrive en
    write_mode="none" sans MCP alors que le groupe est en "ask" (bug #2 de l'audit).
    """
    group = await _call_api("GET", f"/api/agents/{group_id}")
    if not group.get("ok"):
        return group
    group_agent = group.get("agent", {})
    scope = group_agent.get("orchestrator_scope", [])
    if agent_id in scope:
        return {"ok": True, "message": "Deja dans le groupe"}
    scope.append(agent_id)
    result = await _call_api("PUT", f"/api/agents/{group_id}", {"orchestrator_scope": scope})
    # Heriter des reglages du groupe (best-effort — ne pas casser l'ajout si l'heritage echoue)
    inherit = await _inherit_group_settings(agent_id, group_agent)
    if isinstance(result, dict) and inherit.get("inherited"):
        result["inherited"] = inherit["inherited"]
    return result


async def _inherit_group_settings(agent_id: str, group_agent: dict) -> dict:
    """Applique a l'agent les champs securite + connecteurs du groupe. Retourne les champs herites."""
    child = await _call_api("GET", f"/api/agents/{agent_id}")
    child_agent = child.get("agent", {}) if isinstance(child, dict) else {}
    if not child_agent:
        return {}
    group_mcps = group_agent.get("mcps", []) or []
    child_mcps = child_agent.get("mcps", []) or []
    merged_mcps = list(dict.fromkeys([*child_mcps, *group_mcps]))
    payload = {
        "write_mode": group_agent.get("write_mode", "none"),
        "max_writes": group_agent.get("max_writes", 1),
        "blocked_tools": group_agent.get("blocked_tools", []),
        "orchestrable": True,
        "mcps": merged_mcps,
    }
    await _call_api("PUT", f"/api/agents/{agent_id}", payload)
    return {"inherited": payload}


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


async def _test_mcp(mcp_id: str) -> list[TextContent]:
    """Diagnostic d'un connecteur : installe ? actif ? expose des outils ?

    Il n'existe pas de handshake live cote API — on combine le statut (get_mcp) et les
    tool-sets declares pour confirmer qu'un connecteur est pret a etre assigne a un agent.
    """
    mcp = await _call_api("GET", f"/api/mcps/{mcp_id}")
    if isinstance(mcp, dict) and mcp.get("error"):
        return _json_result(mcp)
    mcp_obj = mcp.get("mcp", mcp) if isinstance(mcp, dict) else {}
    enabled = bool(mcp_obj.get("enabled", False))

    tool_sets_resp = await _call_api("GET", f"/api/mcps/{mcp_id}/tool-sets")
    tool_sets = tool_sets_resp.get("tool_sets", []) if isinstance(tool_sets_resp, dict) else []
    tool_count = sum(ts.get("tool_count", 0) for ts in tool_sets)

    ready = enabled and tool_count > 0
    if not enabled:
        diagnosis = "Connecteur desactive — appelez toggle_mcp(enabled=true)."
    elif tool_count == 0:
        diagnosis = "Aucun outil detecte — verifiez la configuration (configure_mcp) ou reinstall_mcp."
    else:
        diagnosis = f"Pret : {len(tool_sets)} tool-set(s), {tool_count} outil(s)."

    return _json_result(
        {
            "ok": True,
            "mcp_id": mcp_id,
            "enabled": enabled,
            "tool_set_count": len(tool_sets),
            "tool_count": tool_count,
            "ready": ready,
            "diagnosis": diagnosis,
        }
    )


# ============================================================================
# Projets (Chat IA / mini-app)
# ============================================================================


async def _list_projects(launcher_only: bool = False) -> list[TextContent]:
    path = "/api/miniapp/projects" + ("?launcher=true" if launcher_only else "")
    result = await _call_api("GET", path)
    if isinstance(result, dict) and "projects" in result:
        projects = result["projects"]
        return _json_result({"ok": True, "projects": projects, "count": len(projects)})
    return _json_result(result)


# ============================================================================
# Automations
# ============================================================================


def _automation_create_payload(args: dict) -> dict:
    """Construit le corps de create_automation (champs optionnels omis si absents)."""
    payload = {
        "name": args["name"],
        "project_id": args["project_id"],
        "inputs": args.get("inputs", {}),
        "trigger": args["trigger"],
    }
    for opt in ("enabled", "hosted", "webhook_secret"):
        if opt in args:
            payload[opt] = args[opt]
    return payload


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
8. Toujours verifier list_agents() avant de creer — eviter les doublons.


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
Toujours verifier list_agents() avant de creer — ne pas dupliquer ces agents.


## Projets (Chat IA / mini-app), Scripts et connecteurs-script

Un Projet est un espace Chat IA : un prompt + des connecteurs MCP prioritaires + un script Python.
- create_project (nom requis) -> update_project (prompt, connectors, model, routing_keywords)
- upload_script pour attacher le code Python. Le script passe une analyse securite (AST + LLM).
  Si le retour a security.blocked=true, NE PAS forcer sans l'accord explicite de l'utilisateur ;
  reappeler upload_script(acknowledge_risk=true) seulement s'il l'accepte (le script reste sandboxe).
- validate_script permet de tester un code AVANT de creer le projet (ne persiste rien).

Secrets (PEP 723) : un script declare ses secrets dans son en-tete. list_project_secrets montre
les secrets DECLARES (id, env_var, configured). set_project_secret(project_id, secret_id, value)
enregistre une valeur — seuls les ids declares sont acceptes. JAMAIS inventer un secret.

Brancher un script comme connecteur (mecanisme d'integration metier) :
1. update_project(available_as_connector=true) sur le projet portant le script.
2. list_connector_scripts pour recuperer son id.
3. update_agent(script_connectors=[project_id]) OU create_group(script_connectors=[project_id]).
   L'agent/groupe peut alors appeler le script comme un outil pendant l'orchestration.


## Automations (declencheurs)

Une automation lance un Projet automatiquement selon un trigger :
- {type:"interval", every_minutes:N} (1 a 10080)  -  {type:"daily", at:"HH:MM"}  -  {type:"webhook"}
create_automation(name, project_id, inputs, trigger). enabled=false par defaut (activer ensuite).
run_automation_now teste immediatement. Pour un trigger webhook : get_automation_webhook_url.
hosted=true = execution cloud h24 (machine eteinte) — reserve au plan Pro x3.


## Verifier un connecteur avant de l'assigner

Apres install_mcp/configure_mcp, valider sans passer par la voix :
- test_mcp(mcp_id) : dit s'il est installe, actif et combien d'outils il expose (ready=true/false).
- list_mcp_tools(mcp_id) : detail des tool-sets et des outils (read/write).
- get_mcp_suggested_agents(mcp_id) : definitions d'agents pretes a l'emploi (securite adaptee).
Un test_mcp avec ready=false -> toggle_mcp(enabled=true) ou reinstall_mcp puis reverifier.


## Connecteur local (package Python custom)

add_mcp n'autorise qu'une whitelist de commandes (npx, uvx, python, python3, node, uv, dotnet) —
pas de chemin absolu d'interpreteur — et interdit PATH/HOME/PYTHONPATH dans env.
"python -m mon_pkg" ne se resout que si Vicsia tourne avec cwd=racine du repo (KO en app packagee).
Contournement fiable et sandbox-safe pour un package livre avec son repo :
  command="uv", args=["run", "--directory", "<chemin_absolu_repo>", "--no-sync", "python", "-m", "<pkg>"]
"uv run --directory" fixe le cwd sans dependre du repertoire de lancement de Vicsia.


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
