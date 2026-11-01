"""The Java inspector: NetBeans, Ant, Maven, Gradle and plain ``javac`` projects.

Works out a project's build system, entry points, packages and Java level, and which build
and run commands the inspected files actually support.

A command is ``supported`` only if an inspected file backs it: an Ant target defined in a
file that was read, a Maven phase of a pom that was read, or a ``javac`` line built from
package declarations and a ``main`` method that were read, with nothing missing. Anything
else is a ``guess``, however plausible. For example, ``ant run`` is a guess until
``nbproject/build-impl.xml``, where NetBeans defines that target, has been inspected.

Values from project files are untrusted: a main class must be a legal Java name and paths
must be plain relative paths, or they are dropped, so nothing hostile reaches a command.

Validation uses only the JDK's ``javac`` and ``java``. Ant, Maven and Gradle are not run:
their build files are themselves programs, and they download plugins. ``javac`` runs with
annotation processing off, so compiling does not run project code; ``java`` does.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from xml.parsers import expat

from ..models import (
    BuildInspection,
    CommandFinding,
    EntryPoint,
    PackageInfo,
    RiskSignal,
    SourceRef,
    ToolRequirement,
)
from ..redact import redact
from .base import (
    InspectionInput,
    InspectorReport,
    ProjectFacts,
    ProjectInspector,
    SourceFile,
    ValidationSpec,
)

SAFE_CLASS_NAME = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
SAFE_RELATIVE_PATH = re.compile(r"^(?!-)(?!.*(?:^|/)\.\.(?:/|$))[\w .+\-/]+$")
SAFE_ENCODING = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,19}$")
#: Packages the JDK provides, so importing them needs nothing on the classpath.
JDK_PACKAGE_PREFIXES = ("java.", "javax.", "jdk.", "org.w3c.", "org.xml.", "org.ietf.", "com.sun.", "sun.")
JAVAFX_PREFIX = "javafx."
TEST_FRAMEWORK_PREFIXES = ("org.junit", "junit.", "org.testng")
JAVA_LANGUAGE = "Java"

#: Ant target by command purpose.
ANT_TARGETS = (("compile", "compile"), ("package", "jar"), ("run", "run"), ("test", "test"))
#: Ant tasks that can run programs, delete files or use the network when a build file runs.
RISKY_ANT_TASKS = frozenset({"exec", "apply", "java", "sshexec", "scp", "ftp", "get", "delete", "script", "taskdef"})

BUILD_FILES: dict[str, str] = {
    "build.xml": "Ant build file",
    "nbproject/project.xml": "NetBeans project descriptor",
    "nbproject/project.properties": "NetBeans project settings (main class, Java level, source folders)",
    "nbproject/build-impl.xml": "NetBeans generated Ant targets (compile, jar, run, test)",
    "manifest.mf": "JAR manifest",
    "src/META-INF/MANIFEST.MF": "JAR manifest",
    "pom.xml": "Maven project file",
    "build.gradle": "Gradle build script",
    "build.gradle.kts": "Gradle build script",
    "settings.gradle": "Gradle settings",
    "settings.gradle.kts": "Gradle settings",
}
REVIEW_ONLY_FILES: tuple[str, ...] = (
    "config.properties", "db.properties", "database.properties", "application.properties", "application.yml",
    "application.yaml", "gradle.properties", "local.properties", "hibernate.cfg.xml", "settings.xml",
    "src/META-INF/persistence.xml", "src/config.properties", "src/db.properties", "src/hibernate.cfg.xml",
)
GRADLE_WRAPPER = "gradlew"

CLASSES_DIR = ".pf-classes"
RUN_MEMORY_LIMIT = "-Xmx256m"
COMPILE_NOTICE = (
    "javac runs with annotation processing off, on a temporary copy of the listed files. "
    "It does not run project code."
)

# --- Java-specific sensitive data ------------------------------------------------------------------

_PLACEHOLDER_CONNECTION_VALUES = frozenset({"", "password", "passwd", "changeme", "changeit", "secret", "test"})
_JDBC_CREDENTIALS = re.compile(
    r"(?i)jdbc:[\w:+-]+:(?://[^/\s\"']*:[^/\s\"'@]+@|[^\"'\s]*[?&;](?:user|username|password)=)"
)
_JDBC_HOST = re.compile(r"(?i)jdbc:[\w+-]+://([\w.-]+)")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "example.com", "db.example.com"})
_CONNECTION_LITERALS = re.compile(r"getConnection\s*\([^;]*?,\s*\"[^\"]+\"\s*,\s*\"([^\"]*)\"")
#: NetBeans fills this with the computer's user name by default.
_IDE_IDENTITY = re.compile(r"(?i)^\s*application\.(?:vendor|homepage)\s*[=:]\s*(?!\$\{)\S")


# --- Java source facts -----------------------------------------------------------------------


def strip_java(source: str) -> str:
    """Blank out comments, string and character literals, keeping every line number."""
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        two = source[i:i + 2]
        if two == "//":
            while i < n and source[i] != "\n":
                i += 1
        elif two == "/*":
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in source[i:end]))
            i = end
        elif source[i:i + 3] == '"""':
            end = source.find('"""', i + 3)
            end = n if end < 0 else end + 3
            out.append('""' + "".join("\n" if ch == "\n" else " " for ch in source[i:end]))
            i = end
        elif c in "\"'":
            i += 1
            while i < n and source[i] != c and source[i] != "\n":
                i += 2 if source[i] == "\\" else 1
            i = min(i + 1, n) if i < n and source[i] == c else i
            out.append(c * 2)
        else:
            out.append(c)
            i += 1
    return "".join(out)


_MAIN = re.compile(
    r"\b((?:(?:public|static|final|synchronized|strictfp)\s+)*)void\s+main\s*\(\s*(?:final\s+)?"
    r"String\s*(?:\[\s*\]\s*\w+|\.\.\.\s*\w+|\w+\s*\[\s*\])\s*\)"
)
_TOKEN = re.compile(r"(?<![.\w])(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)|[{}]")
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;", re.MULTILINE)


def _line_of(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


@dataclass(frozen=True)
class MainMethod:
    class_name: str
    """Class name as ``java`` expects it: ``pkg.Outer`` or ``pkg.Outer$Inner``."""
    line: int
    nested: bool


@dataclass
class JavaSource:
    project_path: str
    file_id: str
    package: str
    mains: list[MainMethod]
    imports: list[str]
    stripped: str
    is_test: bool = False


def parse_java(project_path: str, file_id: str, source: str) -> JavaSource:
    stripped = strip_java(source)
    package_match = _PACKAGE.search(stripped)
    package = package_match.group(1) if package_match else ""
    prefix = f"{package}." if package else ""
    mains: list[MainMethod] = []
    # Only `public static void main(String[])` is an entry point on every Java version.
    main_at = {m.start(): m for m in _MAIN.finditer(stripped) if {"public", "static"} <= set(m.group(1).split())}
    # Walk the braces, remembering which type each one opens, to find the class around a main.
    stack: list[str | None] = []
    pending: str | None = None
    events = sorted([(m.start(), m) for m in _TOKEN.finditer(stripped)] + list(main_at.items()), key=lambda e: e[0])
    for position, match in events:
        if position in main_at and match is main_at[position]:
            names = [s for s in stack if s]
            if stack and stack[-1]:
                mains.append(MainMethod(prefix + "$".join(names), _line_of(stripped, position), len(names) > 1))
        elif match.group(0) == "{":
            stack.append(pending)
            pending = None
        elif match.group(0) == "}":
            if stack:
                stack.pop()
        else:
            pending = match.group(1)
    return JavaSource(project_path, file_id, package, mains, _IMPORT.findall(stripped), stripped)


#: Allows fully qualified names, such as `new java.util.Scanner(...)`.
_QUALIFIER = r"(?:\w+\.)*"
_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("runs_programs", re.compile(r"\bRuntime\s*\.\s*getRuntime\s*\(\s*\)\s*\.\s*exec\b|\bProcessBuilder\b"),
     "starts other programs"),
    ("network", re.compile(rf"\bnew\s+{_QUALIFIER}(?:Socket|ServerSocket|URL|DatagramSocket)\s*\(|\bHttpClient\b|"
                           r"\bopenConnection\b"),
     "uses the network"),
    ("database", re.compile(r"\bDriverManager\s*\.\s*getConnection\b"), "connects to a database"),
    ("changes_files", re.compile(rf"\bnew\s+{_QUALIFIER}(?:FileWriter|FileOutputStream|PrintWriter\s*\(\s*new\s+File|"
                                 r"RandomAccessFile)\b"
                                 r"|\bFiles\s*\.\s*(?:write|writeString|delete|deleteIfExists|move|copy)\b"
                                 r"|\.\s*delete(?:OnExit)?\s*\(\s*\)"),
     "writes or deletes files"),
    ("native_code", re.compile(r"\bSystem\s*\.\s*(?:loadLibrary|load)\s*\("), "loads native code"),
    ("needs_input", re.compile(rf"\bnew\s+{_QUALIFIER}Scanner\s*\(\s*System\s*\.\s*in\b|\bSystem\s*\.\s*in\s*\.\s*read\b|"
                               rf"new\s+{_QUALIFIER}InputStreamReader\s*\(\s*System\s*\.\s*in\b"),
     "reads keyboard input (validation gives it none)"),
    ("needs_display", re.compile(rf"\bnew\s+{_QUALIFIER}J(?:Frame|Dialog)\b|\bJOptionPane\s*\.|"
                                 rf"\bextends\s+{_QUALIFIER}(?:JFrame|JPanel|Application)\b"),
     "opens a window (validation runs without a display)"),
)


def java_risks(source: JavaSource) -> list[RiskSignal]:
    risks = []
    for kind, pattern, detail in _RISK_PATTERNS:
        match = pattern.search(source.stripped)
        if match:
            risks.append(RiskSignal(kind, source.project_path, _line_of(source.stripped, match.start()), detail))
    return risks


# --- build file parsing ------------------------------------------------------------------------


class UnsafeOrInvalidFile(ValueError):
    """A build file was malformed or used XML features that are never processed."""


@dataclass
class XmlNode:
    path: tuple[str, ...]
    attributes: dict[str, str]
    line: int
    text: str = ""

    @property
    def tag(self) -> str:
        return self.path[-1]


def parse_xml(text: str) -> list[XmlNode]:
    """Elements with their paths and lines. Entity declarations are refused, never expanded."""
    parser = expat.ParserCreate()
    nodes: list[XmlNode] = []
    stack: list[XmlNode] = []

    def refuse(*_: object) -> None:
        raise UnsafeOrInvalidFile("it declares XML entities, which are never processed")

    def start(name: str, attributes: dict[str, str]) -> None:
        path = (stack[-1].path if stack else ()) + (name.rpartition(":")[2],)
        node = XmlNode(path, attributes, parser.CurrentLineNumber)
        nodes.append(node)
        stack.append(node)

    def end(_: str) -> None:
        stack.pop()

    def characters(data: str) -> None:
        if stack:
            stack[-1].text += data

    parser.EntityDeclHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    try:
        parser.Parse(text.lstrip("﻿"), True)
    except expat.ExpatError:
        raise UnsafeOrInvalidFile("it is not well-formed XML") from None
    return nodes


def parse_properties(text: str) -> dict[str, tuple[str, int]]:
    """A Java properties file: key -> (value, line). Handles continuation lines."""
    values: dict[str, tuple[str, int]] = {}
    lines = text.lstrip("﻿").splitlines()
    i = 0
    while i < len(lines):
        number = i + 1
        line = lines[i].lstrip()
        i += 1
        if not line or line[0] in "#!":
            continue
        while line.endswith("\\") and not line.endswith("\\\\") and i < len(lines):
            line = line[:-1] + lines[i].lstrip()
            i += 1
        match = re.match(r"((?:[^\s=:\\]|\\.)+)\s*[=:\s]\s*(.*)$", line)
        if match:
            key = match.group(1).replace("\\", "")
            value = re.sub(r"\\(.)", r"\1", match.group(2)).strip()
            values[key] = (value, number)
    return values


# --- what one inspection collects ---------------------------------------------------------------


@dataclass
class _Facts:
    build_systems: list[str] = field(default_factory=list)
    declared_mains: list[tuple[str, SourceRef]] = field(default_factory=list)
    requirements: list[ToolRequirement] = field(default_factory=list)
    source_roots: list[str] = field(default_factory=list)
    test_roots: list[str] = field(default_factory=list)
    encoding: str | None = None
    dependencies: list[SourceRef] = field(default_factory=list)
    commands: list[CommandFinding] = field(default_factory=list)
    risks: list[RiskSignal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _join(root: str, relative: str) -> str:
    return f"{root}/{relative}" if root else relative


def _declare_main(facts: _Facts, value: str, ref: SourceRef) -> None:
    if SAFE_CLASS_NAME.match(value):
        facts.declared_mains.append((value, ref))
    else:
        facts.notes.append(f"{ref.location}: the declared main class is not a legal Java name and was ignored.")


def _require(facts: _Facts, setting: str, value: str, path: str, line: int | None) -> None:
    if SAFE_VERSION.match(value):
        facts.requirements.append(ToolRequirement(setting, value, SourceRef(path, line, f"{setting}={value}")))


def _safe_dir(value: str) -> str | None:
    cleaned = value.strip().rstrip("/")
    return cleaned if cleaned and SAFE_RELATIVE_PATH.match(cleaned) else None


def _analyze_properties(facts: _Facts, root: str, path: str, text: str) -> dict[str, tuple[str, int]]:
    properties = parse_properties(text)
    if "main.class" in properties and properties["main.class"][0]:
        value, line = properties["main.class"]
        _declare_main(facts, value, SourceRef(path, line, "main.class"))
    for setting in ("javac.source", "javac.target"):
        if setting in properties:
            _require(facts, setting, properties[setting][0], path, properties[setting][1])
    platform = properties.get("platform.active")
    if platform and platform[0] != "default_platform":
        _require(facts, "platform.active", platform[0], path, platform[1])
    for key, target in (("src.dir", facts.source_roots), ("test.src.dir", facts.test_roots)):
        directory = _safe_dir(properties.get(key, ("", 0))[0])
        if directory:
            target.append(_join(root, directory))
    encoding = properties.get("source.encoding", ("", 0))[0]
    if SAFE_ENCODING.match(encoding):
        facts.encoding = encoding
    classpath = properties.get("javac.classpath")
    if classpath:
        for entry in re.findall(r"\$\{([\w.\-]+)\}", classpath[0]):
            facts.dependencies.append(SourceRef(path, classpath[1], entry))
        if "${libs." in classpath[0]:
            facts.notes.append("The classpath uses NetBeans library definitions (libs.*), which exist only in a "
                               "NetBeans installation's settings; a plain command line may not resolve them.")
    return properties


@dataclass
class _AntProject:
    targets: dict[str, SourceRef]
    missing_imports: list[str]
    imports_netbeans: bool = False


def _analyze_ant(facts: _Facts, root: str, files: Mapping[str, str]) -> _AntProject | None:
    build_path = _join(root, "build.xml")
    if build_path not in files:
        return None
    targets: dict[str, SourceRef] = {}
    missing: list[str] = []
    imports_netbeans = [False]

    def load(path: str, depth: int = 0) -> None:
        try:
            nodes = parse_xml(files[path])
        except UnsafeOrInvalidFile as exc:
            facts.notes.append(f"{path} was not used: {exc}.")
            return
        for node in nodes:
            if node.tag == "target" and len(node.path) == 2 and node.attributes.get("name"):
                targets.setdefault(node.attributes["name"], SourceRef(path, node.line, f"target {node.attributes['name']}"))
            elif node.tag == "import" and len(node.path) == 2 and node.attributes.get("file"):
                imported = node.attributes["file"]
                resolved = _join(root, imported)
                imports_netbeans[0] = imports_netbeans[0] or imported.startswith("nbproject/")
                if resolved in files and depth < 3:
                    load(resolved, depth + 1)
                elif resolved not in files:
                    missing.append(imported)
            elif node.tag in RISKY_ANT_TASKS and path == build_path:
                facts.risks.append(RiskSignal("build_file_task", path, node.line,
                                              f"this build file uses the <{node.tag}> task when it runs"))

    load(build_path)
    return _AntProject(targets, missing, imports_netbeans[0])


def _ant_commands(facts: _Facts, root: str, ant: _AntProject, netbeans: bool, has_main: bool, has_tests: bool) -> None:
    for purpose, target in ANT_TARGETS:
        command = f"ant {target}"
        if target in ant.targets:
            ref = ant.targets[target]
            if purpose == "run" and netbeans and not has_main:
                facts.commands.append(CommandFinding(purpose, command, "guess", "none", (ref,),
                                                     "the target exists, but no inspected file sets main.class", root))
            elif purpose == "test" and not has_tests:
                facts.commands.append(CommandFinding(purpose, command, "guess", "none", (ref,),
                                                     "the target exists, but no test sources were inspected", root))
            else:
                facts.commands.append(CommandFinding(purpose, command, "supported", "build_file", (ref,),
                                                     f"target '{target}' is defined in {ref.location}", root))
        elif ant.missing_imports:
            imports = ", ".join(sorted(set(ant.missing_imports)))
            facts.commands.append(CommandFinding(purpose, command, "guess", "none", (), (
                f"build.xml imports {imports}, which was not inspected, so target '{target}' is unconfirmed"), root))


def _analyze_netbeans(facts: _Facts, root: str, files: Mapping[str, str]) -> tuple[bool, dict[str, tuple[str, int]]]:
    project_path = _join(root, "nbproject/project.xml")
    settings_path = _join(root, "nbproject/project.properties")
    found = False
    if project_path in files:
        try:
            nodes = parse_xml(files[project_path])
            kinds = [n.text.strip() for n in nodes if n.tag == "type"]
            found = True
            if kinds and "j2seproject" not in kinds[0]:
                facts.notes.append(f"{project_path} describes a NetBeans project of another type; "
                                   "generated Ant targets may differ.")
        except UnsafeOrInvalidFile as exc:
            facts.notes.append(f"{project_path} was not used: {exc}.")
    properties: dict[str, tuple[str, int]] = {}
    if settings_path in files:
        found = True
        properties = _analyze_properties(facts, root, settings_path, files[settings_path])
    return found, properties


def _analyze_manifest(facts: _Facts, path: str, text: str) -> None:
    for number, line in enumerate(text.splitlines(), start=1):
        match = re.match(r"(?i)^main-class\s*:\s*(\S+)\s*$", line)
        if match:
            _declare_main(facts, match.group(1), SourceRef(path, number, "Main-Class"))


def _analyze_maven(facts: _Facts, root: str, path: str, text: str, has_tests: bool) -> None:
    try:
        nodes = parse_xml(text)
    except UnsafeOrInvalidFile as exc:
        facts.notes.append(f"{path} was not used: {exc}.")
        return
    if not nodes or nodes[0].tag != "project":
        facts.notes.append(f"{path} was not used: it does not look like a Maven project file.")
        return
    facts.build_systems.append("maven")
    for index, node in enumerate(nodes):
        value = node.text.strip()
        if node.tag in ("maven.compiler.source", "maven.compiler.target", "maven.compiler.release", "java.version") \
                and "properties" in node.path:
            _require(facts, node.tag, value, path, node.line)
        elif node.tag in ("source", "target", "release") and "configuration" in node.path and "plugin" in node.path:
            _require(facts, f"maven-compiler-plugin {node.tag}", value, path, node.line)
        elif node.tag == "dependency":
            child: dict[str, str] = {}
            for later in nodes[index + 1:]:  # document order: its children come next
                if len(later.path) <= len(node.path):
                    break
                child.setdefault(later.tag, later.text.strip())
            if child.get("artifactId"):
                facts.dependencies.append(SourceRef(path, node.line, f"{child.get('groupId', '?')}:{child['artifactId']}"))
    exec_main: SourceRef | None = None
    for node in nodes:
        if node.tag == "mainClass" and SAFE_CLASS_NAME.match(node.text.strip()):
            ref = SourceRef(path, node.line, "mainClass")
            facts.declared_mains.append((node.text.strip(), ref))
            exec_main = exec_main or ref
    has_exec_plugin = any(n.tag == "artifactId" and n.text.strip() == "exec-maven-plugin" for n in nodes)
    origin = SourceRef(path, nodes[0].line, "Maven project file")
    reason = "standard Maven lifecycle phase of the inspected pom.xml"
    facts.commands.append(CommandFinding("compile", "mvn compile", "supported", "build_file", (origin,), reason, root))
    facts.commands.append(CommandFinding("package", "mvn package", "supported", "build_file", (origin,), reason, root))
    if has_tests:
        facts.commands.append(CommandFinding("test", "mvn test", "supported", "build_file", (origin,), reason, root))
    else:
        facts.commands.append(CommandFinding("test", "mvn test", "guess", "none", (origin,),
                                             "the phase exists, but no test sources were inspected", root))
    if has_exec_plugin and exec_main:
        facts.commands.append(CommandFinding("run", "mvn exec:java", "supported", "build_file", (exec_main,),
                                             "pom.xml configures exec-maven-plugin with a mainClass", root))
    else:
        facts.commands.append(CommandFinding("run", "mvn exec:java", "guess", "none", (origin,),
                                             "pom.xml does not configure exec-maven-plugin with a mainClass", root))


_GRADLE_PLUGIN = re.compile(r"""(?:\bid\s*\(?\s*['"]|apply\s+plugin\s*:\s*['"]|^\s*)(java|application|java-library)['"\s)]""",
                            re.MULTILINE)
_GRADLE_MAIN = re.compile(r"""\bmainClass(?:Name)?\b[^'"\n]*['"]([\w.$]+)['"]""")
_GRADLE_LEVEL = re.compile(r"""\b(sourceCompatibility|targetCompatibility)\s*=?\s*['"]?(?:JavaVersion\.VERSION_)?([\w.]+)['"]?""")
_GRADLE_TOOLCHAIN = re.compile(r"\blanguageVersion\b[^\n]*?\bof\s*\(\s*(\d+)\s*\)")


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments from a build script, keeping lines and string contents."""
    without_block = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)
    return re.sub(r"(?m)(?<![:'\"])//.*$", "", without_block)


def _analyze_gradle(facts: _Facts, root: str, path: str, text: str, has_tests: bool, wrapper: bool) -> None:
    stripped = _strip_comments(text)
    facts.build_systems.append("gradle")
    plugins = {m.group(1) for m in _GRADLE_PLUGIN.finditer(stripped)}
    origin = SourceRef(path, 1, "Gradle build script")
    for match in _GRADLE_LEVEL.finditer(stripped):
        _require(facts, match.group(1), match.group(2).replace("_", "."), path, _line_of(stripped, match.start()))
    for match in _GRADLE_TOOLCHAIN.finditer(stripped):
        _require(facts, "toolchain languageVersion", match.group(1), path, _line_of(stripped, match.start()))
    main_match = _GRADLE_MAIN.search(stripped)
    main_ref = None
    if main_match and SAFE_CLASS_NAME.match(main_match.group(1)):
        main_ref = SourceRef(path, _line_of(stripped, main_match.start()), "mainClass")
        facts.declared_mains.append((main_match.group(1), main_ref))
    tool = "./gradlew" if wrapper else "gradle"
    has_java = bool(plugins & {"java", "application", "java-library"})
    reason = "the Java plugin applied in the inspected build script provides this task"
    if has_java:
        facts.commands.append(CommandFinding("compile", f"{tool} classes", "supported", "build_file", (origin,), reason, root))
        facts.commands.append(CommandFinding("package", f"{tool} jar", "supported", "build_file", (origin,), reason, root))
    if has_java and has_tests:
        facts.commands.append(CommandFinding("test", f"{tool} test", "supported", "build_file", (origin,), reason, root))
    elif has_java:
        facts.commands.append(CommandFinding("test", f"{tool} test", "guess", "none", (origin,),
                                             "the task exists, but no test sources were inspected", root))
    if "application" in plugins and main_ref:
        facts.commands.append(CommandFinding("run", f"{tool} run", "supported", "build_file", (main_ref,),
                                             "the application plugin and a mainClass are in the build script", root))
    else:
        facts.commands.append(CommandFinding("run", f"{tool} run", "guess", "none", (origin,),
                                             "the build script does not apply the application plugin with a mainClass",
                                             root))
    facts.risks.append(RiskSignal("build_file_task", path, None,
                                  "Gradle build scripts are programs: running Gradle executes this file"))


# --- putting the facts together -------------------------------------------------------------------


def _build_root_of(project_path: str) -> str | None:
    for relative in sorted(BUILD_FILES, key=len, reverse=True):
        if project_path == relative:
            return ""
        if project_path.endswith("/" + relative):
            return project_path[:-len(relative) - 1]
    return None


def _source_location_ok(source: JavaSource) -> tuple[bool, str]:
    """Whether a file sits where its package says, and the source root that implies."""
    if "/" not in source.project_path and source.package:
        return False, ""
    folder = source.project_path.rpartition("/")[0]
    expected = source.package.replace(".", "/")
    if not expected:
        return True, folder
    if folder == expected:
        return True, ""
    if folder.endswith("/" + expected):
        return True, folder[:-len(expected) - 1]
    return False, folder


def is_test_source(source: JavaSource, test_roots: Iterable[str]) -> bool:
    path = source.project_path
    if any(path.startswith(root + "/") for root in test_roots):
        return True
    if path.startswith(("test/", "tests/")) or "/src/test/" in f"/{path}":
        return True
    return any(i.startswith(TEST_FRAMEWORK_PREFIXES) for i in source.imports)


def external_imports(sources: Iterable[JavaSource]) -> list[str]:
    """Imports that are neither the JDK's nor the project's own."""
    items = list(sources)
    own = {s.package for s in items if s.package}

    def is_own(name: str) -> bool:
        return any(name == p or name.startswith(p + ".") for p in own)

    return sorted({i for s in items for i in s.imports
                   if not i.startswith(JDK_PACKAGE_PREFIXES) and not is_own(i)})


def _entry_points(facts: _Facts, sources: list[JavaSource]) -> list[EntryPoint]:
    declared: dict[str, list[SourceRef]] = {}
    for name, ref in facts.declared_mains:
        declared.setdefault(name, []).append(ref)
    found = {m.class_name: (s, m) for s in sources if not s.is_test for m in s.mains}
    known_types = {(s.package + "." if s.package else "") + s.project_path.rpartition("/")[2].removesuffix(".java")
                   for s in sources}
    entries = []
    for name in sorted(set(declared) | set(found)):
        refs = tuple(declared.get(name, ()))
        if name in found:
            source, main = found[name]
            ref = SourceRef(source.project_path, main.line, "main method")
            note = "nested class" if main.nested else None
            entries.append(EntryPoint(name, "confirmed" if refs else "source_only", True, ref, refs, note))
        else:
            note = ("the inspected source for this class has no main method" if name in known_types
                    else "the source for this class was not inspected, so its main method is unconfirmed")
            entries.append(EntryPoint(name, "declared_only", False, None, refs, note))
    return entries


def _plain_commands(facts: _Facts, sources: list[JavaSource], entries: list[EntryPoint], unapproved: int) -> None:
    code = [s for s in sources if not s.is_test]
    if not code:
        return
    problems = []
    if unapproved:
        problems.append(f"{unapproved} of the project's Java file(s) were not inspected")
    externals = external_imports(code)
    if externals:
        shown = ", ".join(externals[:5]) + (" and more" if len(externals) > 5 else "")
        problems.append(f"the code imports libraries a plain javac command would not find ({shown})")
    names = " ".join(f'"{s.project_path}"' if " " in s.project_path else s.project_path for s in code)
    evidence = tuple(SourceRef(s.project_path, None, "inspected source file") for s in code[:10])
    if problems:
        reason = "; ".join(problems)
        facts.commands.append(CommandFinding("compile", f"javac -d out {names}", "guess", "none", evidence, reason))
    else:
        facts.commands.append(CommandFinding(
            "compile", f"javac -d out {names}", "supported", "source_structure", evidence,
            "built from the inspected source files; no build file states this command"))
    for entry in entries:
        if not entry.found_in_source or (entry.note == "nested class"):
            continue
        command = f"java -cp out {entry.name}"
        if problems:
            facts.commands.append(CommandFinding("run", command, "guess", "none", (entry.source,), "; ".join(problems)))
        else:
            facts.commands.append(CommandFinding(
                "run", command, "supported", "source_structure", (entry.source,),
                "a main method was found in the inspected source; run after the javac command"))


def _netbeans_jar_command(facts: _Facts, netbeans_roots: dict[str, dict[str, tuple[str, int]]]) -> None:
    """NetBeans can package a runnable JAR; only supported when both ends of that are backed."""
    for root, properties in netbeans_roots.items():
        jar_setting = properties.get("dist.jar")
        if not jar_setting:
            continue
        dist_dir = properties.get("dist.dir", ("", 0))[0]
        jar = jar_setting[0].replace("${dist.dir}", dist_dir)
        if "${" in jar or not SAFE_RELATIVE_PATH.match(jar):
            continue
        settings = _join(root, "nbproject/project.properties")
        ref = SourceRef(settings, jar_setting[1], "dist.jar")
        package = next((c for c in facts.commands
                        if c.purpose == "package" and c.command == "ant jar" and c.working_dir == root), None)
        has_main = any(r.project_path == settings for _, r in facts.declared_mains)
        command = f'java -jar "{jar}"' if " " in jar else f"java -jar {jar}"
        if package and package.status == "supported" and has_main:
            facts.commands.append(CommandFinding("run", command, "supported", "build_file", (ref, *package.evidence),
                                                 "run after 'ant jar'; dist.jar and main.class are in the inspected "
                                                 "settings", root))
        else:
            facts.commands.append(CommandFinding("run", command, "guess", "none", (ref,),
                                                 "needs 'ant jar' to be confirmed and main.class to be set", root))


def _packages(sources: list[JavaSource]) -> list[PackageInfo]:
    grouped: dict[str, list[JavaSource]] = {}
    for source in sources:
        grouped.setdefault(source.package, []).append(source)
    return [PackageInfo(name, len(items), all(_source_location_ok(s)[0] for s in items))
            for name, items in sorted(grouped.items())]


def _command_order(command: CommandFinding) -> tuple[int, int]:
    purposes = {"compile": 0, "package": 1, "run": 2, "test": 3}
    return purposes.get(command.purpose, 9), 0 if command.status == "supported" else 1


class JavaInspector(ProjectInspector):
    name = "java"
    build_files = BUILD_FILES
    review_only_files = REVIEW_ONLY_FILES
    root_marker_files = frozenset({"build.xml", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
                                   "manifest.mf"})
    root_marker_dirs = frozenset({"nbproject"})
    presence_checks = (GRADLE_WRAPPER,)
    config_file_suffixes = (".mf", ".gradle", ".kts")
    allowed_tools = frozenset({"javac", "java"})
    scratch_dirs = (CLASSES_DIR,)

    def detects(self, facts: ProjectFacts) -> bool:
        return any(p.casefold().endswith(".java") for p in facts.member_paths) \
            or bool(facts.marker_dirs & self.root_marker_dirs) \
            or bool(facts.marker_files & (self.root_marker_files - {"manifest.mf"}))

    def inspect(self, inputs: InspectionInput) -> InspectorReport:
        facts = _Facts()
        by_root: dict[str, dict[str, str]] = {}
        for path, text in inputs.build_files.items():
            root = _build_root_of(path)
            if root is not None:
                by_root.setdefault(root, {})[path] = text

        netbeans_roots: dict[str, dict[str, tuple[str, int]]] = {}
        for root, files in by_root.items():
            found, properties = _analyze_netbeans(facts, root, files)
            if found:
                netbeans_roots[root] = properties
            for name in ("manifest.mf", "src/META-INF/MANIFEST.MF"):
                if _join(root, name) in files:
                    _analyze_manifest(facts, _join(root, name), files[_join(root, name)])

        sources = [parse_java(s.project_path, s.file_id, s.text) for s in inputs.sources if s.language == JAVA_LANGUAGE]
        for source in sources:
            source.is_test = is_test_source(source, facts.test_roots)
            if not source.is_test:
                facts.risks.extend(java_risks(source))
        has_tests = any(s.is_test for s in sources)

        for root, files in by_root.items():
            pom = _join(root, "pom.xml")
            if pom in files:
                _analyze_maven(facts, root, pom, files[pom], has_tests)
            for name in ("build.gradle", "build.gradle.kts"):
                gradle = _join(root, name)
                if gradle in files:
                    _analyze_gradle(facts, root, gradle, files[gradle], has_tests, GRADLE_WRAPPER in inputs.present)

        entries = _entry_points(facts, sources)
        for root, files in by_root.items():
            ant = _analyze_ant(facts, root, files)
            if ant is not None:
                # NetBeans keeps main.class in its settings, whichever of its files were approved.
                netbeans = root in netbeans_roots or ant.imports_netbeans
                facts.build_systems.append("netbeans" if netbeans else "ant")
                has_main = any(n for n, r in facts.declared_mains
                               if r.project_path.startswith(_join(root, "nbproject")))
                _ant_commands(facts, root, ant, netbeans, has_main, has_tests)
            elif root in netbeans_roots:
                facts.build_systems.append("netbeans")
                facts.notes.append("The NetBeans project files were inspected, but its build.xml was not.")

        unapproved = sum(p.casefold().endswith(".java") for p in inputs.unapproved_paths)
        _plain_commands(facts, sources, entries, unapproved)
        _netbeans_jar_command(facts, netbeans_roots)

        source_roots = list(dict.fromkeys(facts.source_roots))
        for source in sources:
            ok, implied = _source_location_ok(source)
            if ok and not source.is_test and (implied or ".") not in source_roots:
                source_roots.append(implied or ".")
        misplaced = [s.project_path for s in sources if not _source_location_ok(s)[0]]
        if misplaced:
            facts.notes.append(f"{len(misplaced)} file(s) are not in the folder their package declaration requires.")
        if any(i.startswith(JAVAFX_PREFIX) for s in sources for i in s.imports):
            facts.notes.append("The code imports JavaFX, which JDK 11 and later do not include; it needs a separate "
                               "install.")
        if not sources:
            facts.notes.append("No Java sources were inspected, so main methods and packages are unconfirmed.")
        elif not facts.requirements:
            facts.notes.append("No inspected file states a required Java version.")
        return InspectorReport(
            build_systems=list(dict.fromkeys(facts.build_systems)), entry_points=entries, packages=_packages(sources),
            source_roots=source_roots, test_roots=list(dict.fromkeys(facts.test_roots)), requirements=facts.requirements,
            dependencies=[SourceRef(r.project_path, r.line, redact(r.detail)[0]) for r in facts.dependencies],
            commands=sorted(facts.commands, key=_command_order), risks=facts.risks, notes=facts.notes,
            options={"encoding": facts.encoding} if facts.encoding else {},
        )

    def sensitive_rules(self, line: str) -> Iterable[tuple[str, str, str]]:
        if _JDBC_CREDENTIALS.search(line):
            yield "jdbc_credentials", "high", "a database URL appears to include a user name or password"
        connection = _CONNECTION_LITERALS.search(line)
        if connection and connection.group(1).strip().casefold() not in _PLACEHOLDER_CONNECTION_VALUES \
                and not connection.group(1).startswith(("${", "%", "<")):
            yield "hardcoded_credential", "high", "a database connection is opened with literal credentials"
        host = _JDBC_HOST.search(line)
        if host and host.group(1).casefold() not in _LOCAL_HOSTS:
            yield "database_host", "medium", "a database URL names a non-local host"
        if _IDE_IDENTITY.match(line):
            yield "ide_identity", "medium", "an IDE vendor or homepage setting, often the computer's user name"

    # --- validation ------------------------------------------------------------------------------

    def validation_actions(self) -> tuple[str, ...]:
        return ("compile", "run")

    def runs_project_code(self, action: str) -> bool:
        return action != "compile"

    def needs_entry_point(self, action: str) -> bool:
        return action == "run"

    def is_validation_source(self, language: str, project_path: str) -> bool:
        return language == JAVA_LANGUAGE

    def prepare_validation(self, action: str, sources: tuple[SourceFile, ...], inspection: BuildInspection,
                           entry_point: str | None) -> ValidationSpec:
        parsed = [parse_java(s.project_path, s.file_id, s.text) for s in sources]
        code = [s for s in parsed if not is_test_source(s, inspection.test_roots)]
        warnings = [COMPILE_NOTICE]
        if action == "run":
            warnings += [f"{r.project_path} line {r.line or '?'}: {r.detail}" for r in inspection.risks
                         if r.kind != "build_file_task"]
        externals = external_imports(code)
        if externals:
            warnings.append("These imports are not provided by the JDK and will not be available, so compiling will "
                            "probably fail: " + ", ".join(externals[:8]) + (" and more" if len(externals) > 8 else ""))
        not_copied = sum(path.casefold().endswith(".java") for path in inspection.not_inspected)
        if not_copied:
            warnings.append(f"{not_copied} Java file(s) were not approved and will not be copied.")
        encoding = inspection.options.get("encoding", "")
        options = {"encoding": encoding if SAFE_ENCODING.match(encoding) else "UTF-8"}
        return ValidationSpec(tuple(s.project_path for s in code), entry_point, options, tuple(warnings))

    def build_commands(self, action: str, files: tuple[str, ...], entry_point: str | None,
                       options: Mapping[str, str]) -> tuple[tuple[str, ...], ...]:
        compile_command = ("javac", "-proc:none", "-encoding", options["encoding"], "-d", CLASSES_DIR, *files)
        if action == "compile":
            return (compile_command,)
        return (compile_command, ("java", RUN_MEMORY_LIMIT, "-Djava.awt.headless=true", "-Duser.home=.",
                                  "-cp", CLASSES_DIR, str(entry_point)))

    def stage_of(self, command: tuple[str, ...]) -> str:
        return "compile" if command[0] == "javac" else "run"

    def is_safe_copy_path(self, project_path: str) -> bool:
        return project_path.endswith(".java")

    def is_safe_entry_point(self, entry_point: str) -> bool:
        return bool(SAFE_CLASS_NAME.match(entry_point))

    def options_are_safe(self, options: Mapping[str, str]) -> bool:
        return set(options) == {"encoding"} and bool(SAFE_ENCODING.match(options["encoding"]))

    def version_args(self, tool: str) -> tuple[str, ...]:
        return ("-version",)

    def failure_hints(self, stderr: str) -> Iterable[str]:
        if "HeadlessException" in stderr:
            yield "The program tried to open a window, and validation runs without a display. Check it by hand."
        if "NoSuchElementException" in stderr or "No line found" in stderr:
            yield "The program may have been waiting for keyboard input, which validation does not provide."
        if "package " in stderr and "does not exist" in stderr:
            yield "A library is missing. Only the approved sources are copied; libraries are not."
