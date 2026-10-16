// Politiques d'exécution des outils d'ORION, évaluées par jarvis/tools/policy.py ([tools.policy] engine).
// Équivalent de jarvis.tools.permissions.PermissionManager + jarvis.profiles (rôles), plus des règles de contexte :
// origine non fiable, modèle non fiable, appareil interdit, lien vers un exécutable. Tout ce qui n'est pas permis
// est refusé (règle par défaut de Cedar) ; un forbid l'emporte toujours sur un permit. Le contexte est construit par
// le Core, jamais par le LLM. L'ordre compte : policy.py associe chaque règle à son @id dans l'ordre du fichier.

@id("owner_all")
permit (principal in Role::"owner", action == Action::"execute", resource);

@id("adult_all_but_mail")
permit (principal in Role::"adult", action == Action::"execute", resource)
unless { resource in Category::"mail" };

@id("child_categories")
permit (principal in Role::"child", action == Action::"execute", resource)
when {
  resource in Category::"info" || resource in Category::"home" ||
  resource in Category::"media" || resource in Category::"schedule"
};

@id("guest_categories")
permit (principal in Role::"guest", action == Action::"execute", resource)
when { resource in Category::"info" || resource in Category::"home" || resource in Category::"media" };

@id("restricted_tool")
forbid (principal, action, resource)
when { resource.risk == "restricted" };

@id("unknown_risk")
forbid (principal, action, resource)
unless { resource.risk == "safe" || resource.risk == "confirm" || resource.risk == "restricted" };

@id("confirmation_required")
forbid (principal, action, resource)
when { resource.risk == "confirm" }
unless { context.confirmed };

@id("confirm_reserved_to_adults")
forbid (principal, action, resource)
when { resource.risk == "confirm" }
unless { principal in Role::"owner" || principal in Role::"adult" };

@id("forget_all_owner_only")
forbid (principal, action, resource == Tool::"forget")
when { context.forget_all }
unless { principal in Role::"owner" };

@id("untrusted_origin")
forbid (principal, action, resource)
unless { context.origin == "user" || context.origin == "routine" || context.origin == "admin" };

@id("untrusted_model")
forbid (principal, action, resource)
when { context.origin == "user" && !context.llm_trusted }
unless {
  resource in Category::"info" || resource in Category::"home" ||
  resource in Category::"media" || resource in Category::"schedule"
};

@id("forbidden_device")
forbid (principal, action, resource)
when { context.device_forbidden };

@id("executable_link")
forbid (principal, action, resource == Tool::"open_url")
when { context.url_executable };
