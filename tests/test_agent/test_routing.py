import pytest

from src.agent.routing import (
    AstroStrategy,
    NextAppRouterStrategy,
    NextPagesRouterStrategy,
    RemixStrategy,
    RoutingStrategy,
    SvelteKitStrategy,
    get_strategy,
)


class TestNextAppRouter:
    def test_page_to_route(self):
        s = NextAppRouterStrategy()
        assert s.file_to_route("app/page.tsx") == "/"
        assert s.file_to_route("src/app/dashboard/page.tsx") == "/dashboard"
        assert s.file_to_route("components/Button.tsx") is None

    def test_layout_and_global(self):
        s = NextAppRouterStrategy()
        assert s.is_layout_file("app/dashboard/layout.tsx") == (True, "/dashboard")
        assert s.is_global_file("app/globals.css") is True

    def test_discover(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "page.tsx").write_text("x")
        (tmp_path / "app" / "about").mkdir()
        (tmp_path / "app" / "about" / "page.tsx").write_text("x")
        assert NextAppRouterStrategy().discover_all_routes(tmp_path) == ["/", "/about"]

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("app/(marketing)/about/page.tsx", "/about"),
            ("src/app/(marketing)/about/page.tsx", "/about"),
            ("app/(group)/page.tsx", "/"),
            ("app/@modal/photo/page.tsx", "/photo"),  # parallel slot dropped
            ("app/(.)photo/page.tsx", None),  # intercepting -> non-screenshottable
            ("app/(..)photo/page.tsx", None),
            ("app/(...)photo/page.tsx", None),
            ("app/api/x/route.ts", None),  # API route handler
            ("app/dashboard/route.tsx", None),
            ("app/dashboard/page.tsx", "/dashboard"),
        ],
    )
    def test_route_group_slot_intercepting_and_route_handlers(self, path, expected):
        assert NextAppRouterStrategy().file_to_route(path) == expected

    def test_layout_strips_route_group(self):
        s = NextAppRouterStrategy()
        assert s.is_layout_file("app/(marketing)/layout.tsx") == (True, "/")
        assert s.is_layout_file("app/(marketing)/about/layout.tsx") == (True, "/about")

    def test_layout_intercepting_is_not_layout(self):
        assert NextAppRouterStrategy().is_layout_file("app/(.)photo/layout.tsx") == (False, "")

    def test_discover_drops_route_groups(self, tmp_path):
        (tmp_path / "app" / "(marketing)" / "about").mkdir(parents=True)
        (tmp_path / "app" / "(marketing)" / "about" / "page.tsx").write_text("x")
        (tmp_path / "app" / "api" / "x").mkdir(parents=True)
        (tmp_path / "app" / "api" / "x" / "route.ts").write_text("x")
        assert NextAppRouterStrategy().discover_all_routes(tmp_path) == ["/about"]


class TestNextPagesRouter:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("pages/index.tsx", "/"),
            ("src/pages/about.tsx", "/about"),
            ("pages/blog/index.tsx", "/blog"),
            ("pages/_app.tsx", None),
            ("pages/api/users.ts", None),
            ("pages/blog/[slug].tsx", None),  # dynamic, skipped
        ],
    )
    def test_file_to_route(self, path, expected):
        assert NextPagesRouterStrategy().file_to_route(path) == expected

    def test_app_and_config_are_global(self):
        s = NextPagesRouterStrategy()
        assert s.is_global_file("pages/_app.tsx") is True
        assert s.is_global_file("next.config.js") is True


class TestAstro:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("src/pages/index.astro", "/"),
            ("src/pages/about.astro", "/about"),
            ("src/pages/blog/index.md", "/blog"),
            ("src/pages/[id].astro", None),
            ("src/components/Card.astro", None),
        ],
    )
    def test_file_to_route(self, path, expected):
        assert AstroStrategy().file_to_route(path) == expected


class TestSvelteKit:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("src/routes/+page.svelte", "/"),
            ("src/routes/about/+page.svelte", "/about"),
            ("src/routes/(app)/dash/+page.svelte", "/dash"),  # route group dropped
            ("src/routes/blog/[slug]/+page.svelte", None),  # dynamic
        ],
    )
    def test_file_to_route(self, path, expected):
        assert SvelteKitStrategy().file_to_route(path) == expected

    def test_layout(self):
        assert SvelteKitStrategy().is_layout_file("src/routes/about/+layout.svelte") == (True, "/about")

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("src/routes/[[lang]]/about/+page.svelte", "/about"),  # optional param dropped
            ("src/routes/[...rest]/+page.svelte", "/"),  # rest param dropped
            ("src/routes/blog/[slug]/+page.svelte", None),  # required param dynamic
            ("src/routes/about/+page.server.ts", "/about"),  # server page discovered
            ("src/routes/about/+page.ts", "/about"),
            ("src/routes/about/+page.md", "/about"),
        ],
    )
    def test_broadened_pages_and_params(self, path, expected):
        assert SvelteKitStrategy().file_to_route(path) == expected

    def test_discover_includes_server_pages(self, tmp_path):
        (tmp_path / "src" / "routes" / "about").mkdir(parents=True)
        (tmp_path / "src" / "routes" / "about" / "+page.server.ts").write_text("x")
        (tmp_path / "src" / "routes" / "+page.svelte").write_text("x")
        assert SvelteKitStrategy().discover_all_routes(tmp_path) == ["/", "/about"]

    def test_source_extensions(self):
        assert ".svelte" in SvelteKitStrategy().source_extensions
        assert ".astro" in AstroStrategy().source_extensions
        assert RoutingStrategy().source_extensions == (".ts", ".tsx", ".js", ".jsx")


class TestRemix:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("app/routes/_index.tsx", "/"),
            ("app/routes/about.tsx", "/about"),
            ("app/routes/blog.post.tsx", "/blog/post"),
            ("app/routes/users/route.tsx", "/users"),
            ("app/routes/users.$id.tsx", None),  # dynamic
        ],
    )
    def test_file_to_route(self, path, expected):
        assert RemixStrategy().file_to_route(path) == expected

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("app/routes/blog_.post.tsx", "/blog/post"),  # layout opt-out trailing _
            ("app/routes/concerts.trending[.]json.tsx", "/concerts/trending.json"),  # escaped dot
            ("app/routes/($lang).about.tsx", "/about"),  # optional segment dropped
            ("app/routes/_index.tsx", "/"),
            ("app/routes/_auth.login.tsx", "/login"),  # pathless layout dropped
            ("app/routes/blog.post.tsx", "/blog/post"),  # plain dotted still works
            ("app/routes/files.$.tsx", None),  # splat is dynamic
            ("app/routes/_index/index.tsx", "/"),  # folder index form
        ],
    )
    def test_flat_route_parsing(self, path, expected):
        assert RemixStrategy().file_to_route(path) == expected


class TestSpaFallback:
    def test_yields_nothing(self, tmp_path):
        s = RoutingStrategy()
        assert s.file_to_route("src/App.tsx") is None
        assert s.discover_all_routes(tmp_path) == []
        assert s.is_page_file("src/App.tsx") is False


class TestGetStrategy:
    def test_next_app_when_app_dir_present(self, tmp_path):
        (tmp_path / "app").mkdir()
        assert get_strategy("next", tmp_path).name == "next-app"

    def test_next_pages_when_only_pages_dir(self, tmp_path):
        (tmp_path / "pages").mkdir()
        assert get_strategy("next", tmp_path).name == "next-pages"

    def test_next_defaults_to_app(self, tmp_path):
        assert get_strategy("next", tmp_path).name == "next-app"

    @pytest.mark.parametrize(
        "framework,expected",
        [
            ("astro", "astro"),
            ("sveltekit", "sveltekit"),
            ("remix", "remix"),
            ("vite", "spa"),
            ("cra", "spa"),
            ("spa", "spa"),
            ("unknown", "spa"),
        ],
    )
    def test_dispatch(self, framework, expected):
        assert get_strategy(framework).name == expected

    def test_prompt_context_has_required_keys(self):
        for fw in ["next", "astro", "sveltekit", "remix", "vite"]:
            ctx = get_strategy(fw).prompt_context()
            assert {"routing_description", "route_example_file", "route_file_examples"} <= ctx.keys()
