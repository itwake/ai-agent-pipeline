#!/usr/bin/env groovy
/*
 * AI agent pipeline.
 *
 * Runs an AI agent skill on a Linux build node. The agent lives in this
 * repository (pipeline_agent/, standard-library Python only). Each loop step
 * is one `python3 -m pipeline_agent run|resume` process; when the agent asks
 * the user something the process exits, the pipeline shows a Jenkins `input`
 * form, writes the answers to a file and resumes the same session.
 *
 * Required plugins: Pipeline, Git, Credentials Binding, Folder Properties.
 * Pipeline Utility Steps is NOT needed: state is exchanged through a flat
 * properties file parsed here with plain String methods.
 *
 * Folder Properties (non-secret configuration, all optional unless noted):
 *   GIT_CREDENTIALS_ID         username/password credential for the git server (default ghe-token)
 *   COPILOT_CREDENTIALS_ID     secret text credential holding a GitHub token with Copilot access (default copilot-github-token)
 *   AI_PLATFORM_CREDENTIALS_ID username/password credential for AI Platform (default ai-platform-credentials)
 *   AI_PLATFORM_CHAT_HOST, AI_PLATFORM_CHAT_URI, AI_PLATFORM_RESPONSES_URI, AI_PLATFORM_RESPONSES_HOST,
 *   AI_PLATFORM_IB2B_HOST, AI_PLATFORM_IB2B_URI, AI_PLATFORM_USERCASE, AI_PLATFORM_TRUST_TOKEN_HEADER,
 *   AI_PLATFORM_TRACKING_PREFIX  AI Platform endpoints (required when AI_PROVIDER=ai_platform)
 *   GIT_SERVER_HOST            host name of the company git server, mentioned to the agent
 *   GIT_COMMIT_NAME, GIT_COMMIT_EMAIL  identity for the agent's commits
 *   BUILD_TOOLS_ROOT           root of the tool installations (default /opt/build_tools)
 *   BUILD_TOOLS                comma-separated tool directories to put on PATH as <root>/<tool>/latest/bin
 *   JAVA_HOME_DIR              JAVA_HOME for the agent (default <root>/jdk/latest)
 *   SKILLS_REPO_URL, SKILLS_REPO_REF  optional extra skills repository to overlay on skills/
 */

properties([
    parameters([
        choice(name: 'AI_PROVIDER', choices: ['github_copilot', 'ai_platform', 'fake'],
               description: 'Model provider. "fake" replays config/fake-script.json and needs no credentials; use it to test the pipeline wiring.'),
        choice(name: 'MODEL', choices: ['gpt-6-sol', 'gpt-6-astra', 'gpt-6-luna', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.4'],
               description: 'GitHub Copilot: gpt-6-sol, gpt-6-astra, gpt-6-luna, gpt-5.6-sol, gpt-5.6-luna, gpt-5.6-terra. AI Platform: gpt-5.4, gpt-5.6-sol, gpt-5.6-luna, gpt-5.6-terra.'),
        choice(name: 'THINKING_LEVEL', choices: ['high', 'medium', 'low', 'xhigh', 'max'],
               description: 'Reasoning effort passed to the model.'),
        choice(name: 'CONTEXT_SIZE', choices: ['default', '128k', '256k', '512k', '1m'],
               description: 'Context window the agent may use. "default" takes the model catalog value; a smaller value compacts the conversation earlier.'),
        choice(name: 'SKILL', choices: ['fix-foss', 'pipeline-smoke', 'none'],
               description: 'Skill to activate (a directory under skills/).'),
        text(name: 'PROMPT', defaultValue: '',
             description: 'The task. For fix-foss include the repository HTTPS URL, the base branch if not the default, and the findings (component, version, fixed version or CVE) or where the report is.'),
        string(name: 'MAX_ITERATIONS', defaultValue: '200', description: 'Model calls allowed before the pipeline asks whether to continue.'),
        string(name: 'INPUT_TIMEOUT_MINUTES', defaultValue: '60', description: 'How long a question may wait for an answer before the build is aborted.'),
        booleanParam(name: 'DRY_RUN', defaultValue: false, description: 'Let the agent do everything except push or open pull requests.'),
        string(name: 'AGENT_LABEL', defaultValue: 'linux', description: 'Label of the node to run on (empty = any).'),
    ]),
])

// ---------------------------------------------------------------------------
// helpers (plain Groovy; nothing here needs script approval)
// ---------------------------------------------------------------------------

@NonCPS
String unescapeValue(String value) {
    StringBuilder out = new StringBuilder()
    int i = 0
    int length = value.length()
    while (i < length) {
        String c = value.substring(i, i + 1)
        if (c == '\\' && i + 1 < length) {
            String n = value.substring(i + 1, i + 2)
            if (n == 'n') { out.append('\n'); i += 2; continue }
            if (n == '\\') { out.append('\\'); i += 2; continue }
        }
        out.append(c)
        i++
    }
    return out.toString()
}

@NonCPS
String escapeValue(String value) {
    if (value == null) { return '' }
    return value.replace('\\', '\\\\').replace('\r', '').replace('\n', '\\n')
}

@NonCPS
Map parseProperties(String text) {
    Map result = [:]
    for (String line : text.split('\n')) {
        int idx = line.indexOf('=')
        if (idx > 0) {
            result[line.substring(0, idx).trim()] = unescapeValue(line.substring(idx + 1))
        }
    }
    return result
}

Map readAgentState(String stateDir) {
    String path = "${stateDir}/state.properties"
    if (!fileExists(path)) { return [:] }
    return parseProperties(readFile(file: path, encoding: 'UTF-8'))
}

int contextTokensFor(String size) {
    switch ((size ?: 'default').toLowerCase()) {
        case '128k': return 128000
        case '256k': return 256000
        case '512k': return 512000
        case '1m':   return 1000000
        default:     return 0
    }
}

String folderProp(String name, String fallback) {
    String value = env[name]
    return (value != null && value.trim()) ? value.trim() : fallback
}

List credentialsFor(String provider) {
    List creds = [
        usernamePassword(credentialsId: folderProp('GIT_CREDENTIALS_ID', 'ghe-token'),
                         usernameVariable: 'GIT_USERNAME', passwordVariable: 'GIT_PASSWORD'),
    ]
    if (provider == 'github_copilot') {
        creds << string(credentialsId: folderProp('COPILOT_CREDENTIALS_ID', 'copilot-github-token'), variable: 'COPILOT_GITHUB_TOKEN')
    } else if (provider == 'ai_platform') {
        creds << usernamePassword(credentialsId: folderProp('AI_PLATFORM_CREDENTIALS_ID', 'ai-platform-credentials'),
                                  usernameVariable: 'AI_PLATFORM_USERNAME', passwordVariable: 'AI_PLATFORM_PASSWORD')
    }
    return creds
}

/** Render the agent's questions as a Jenkins input form and return the answers as properties text. */
String askUser(Map state, int timeoutMinutes) {
    int count = Integer.parseInt(state['QUESTION_COUNT'] ?: '0')
    List form = []
    for (int i = 0; i < count; i++) {
        String header = state["Q${i}_HEADER"] ?: "Question ${i + 1}"
        String question = state["Q${i}_TEXT"] ?: ''
        int optionCount = Integer.parseInt(state["Q${i}_OPTION_COUNT"] ?: '0')
        boolean custom = (state["Q${i}_CUSTOM"] ?: 'true') == 'true'
        if (optionCount > 0) {
            List labels = []
            List notes = []
            for (int j = 0; j < optionCount; j++) {
                String label = state["Q${i}_OPT${j}_LABEL"] ?: "option ${j + 1}"
                String desc = state["Q${i}_OPT${j}_DESC"] ?: ''
                labels << label
                if (desc) { notes << "${label}: ${desc}" }
            }
            if (custom) { labels << '[other: see text answer]' }
            String description = "${header}: ${question}" + (notes ? '\n' + notes.join('\n') : '')
            form << choice(name: "Q${i}", choices: labels.join('\n'), description: description)
            if (custom) {
                form << string(name: "Q${i}_TEXT", defaultValue: '',
                               description: "Free-text answer for '${header}' (used when it is not empty or when [other] is chosen)")
            }
        } else {
            form << text(name: "Q${i}", defaultValue: '', description: "${header}: ${question}")
        }
    }
    def answer = timeout(time: timeoutMinutes, unit: 'MINUTES') {
        input(message: "The agent has ${count} question(s) for you", parameters: form)
    }
    Map values = (answer instanceof Map) ? answer : ['Q0': answer]
    StringBuilder out = new StringBuilder()
    for (String key : values.keySet()) {
        out.append(key).append('=').append(escapeValue(values[key] == null ? '' : values[key].toString())).append('\n')
    }
    return out.toString()
}

/** Drive run/resume until the agent completes or fails. Returns the final state map. */
Map runAgentLoop(String commonArgs, String stateDir, String promptFile, int inputTimeoutMinutes) {
    String mode = "run --prompt-file '${promptFile}'"
    int round = 0
    while (true) {
        round++
        echo "agent round ${round}: ${mode.tokenize(' ')[0]}"
        int rc = sh(returnStatus: true, script: "python3 -m pipeline_agent ${mode} ${commonArgs}")
        Map state = readAgentState(stateDir)
        String status = state['STATUS'] ?: ''
        echo "agent round ${round} finished: status=${status} exit=${rc} iterations=${state['ITERATIONS'] ?: '?'}"
        if (status == 'waiting_for_question') {
            String answersFile = "${stateDir}/answers-${round}.properties"
            writeFile file: answersFile, text: askUser(state, inputTimeoutMinutes), encoding: 'UTF-8'
            mode = "resume --answers-file '${answersFile}'"
            continue
        }
        if (status == 'max_iterations') {
            def more = timeout(time: inputTimeoutMinutes, unit: 'MINUTES') {
                input(message: "The agent used its ${state['ITERATIONS']} allowed model calls without finishing. Allow more?",
                      parameters: [string(name: 'MORE_ITERATIONS', defaultValue: '100', description: 'Additional model calls, or 0 to stop here')])
            }
            String extra = more.toString().trim()
            if (!extra || extra == '0') {
                error "Stopped at the iteration limit; the partial report is archived under .agent/"
            }
            mode = "resume --more-iterations ${extra}"
            continue
        }
        if (status == 'completed' && rc == 0) {
            return state
        }
        error "Agent run failed (status=${status}, exit=${rc}): ${state['ERROR'] ?: 'see the log above'}"
    }
}

// ---------------------------------------------------------------------------
// pipeline body
// ---------------------------------------------------------------------------

def pipelineBody() {
    withFolderProperties {
        String ws = pwd()
        String stateDir = "${ws}/.agent"
        String agentWorkspace = "${ws}/agent-ws"
        String promptFile = "${stateDir}/prompt.txt"
        String provider = params.AI_PROVIDER ?: 'github_copilot'
        String toolsRoot = folderProp('BUILD_TOOLS_ROOT', '/opt/build_tools')
        String defaultTools = 'python,git,gitlfs,jdk,maven,gradle,apache-ant,apache-groovy,gh,jq,yq,nodejs,go,ruby,' +
                              'nexus-iq-cli,sonar-scanner,kubectl,helm,awscli2,docker-ce,docker-compose,terraform,allure'
        List toolDirs = []
        for (String tool : folderProp('BUILD_TOOLS', defaultTools).split(',')) {
            String name = tool.trim()
            if (name) { toolDirs << "${toolsRoot}/${name}/latest/bin" }
        }
        String toolPath = toolDirs.join(':')
        String javaHome = folderProp('JAVA_HOME_DIR', "${toolsRoot}/jdk/latest")
        String gitHost = folderProp('GIT_SERVER_HOST', '')
        String commitName = folderProp('GIT_COMMIT_NAME', 'ai-agent-pipeline')
        String commitEmail = folderProp('GIT_COMMIT_EMAIL', 'ai-agent-pipeline@noreply.local')
        int inputTimeout = Integer.parseInt(params.INPUT_TIMEOUT_MINUTES ?: '60')
        List skillDirs = ["${ws}/skills"]

        List baseEnv = [
            "PATH=${toolPath}:${env.PATH}",
            "JAVA_HOME=${javaHome}",
            "PYTHONPATH=${ws}",
            "PYTHONUNBUFFERED=1",
            "PYTHONDONTWRITEBYTECODE=1",
            "GIT_TERMINAL_PROMPT=0",
            "GIT_ASKPASS=${ws}/scripts/git-askpass.sh",
            "GIT_AUTHOR_NAME=${commitName}",
            "GIT_AUTHOR_EMAIL=${commitEmail}",
            "GIT_COMMITTER_NAME=${commitName}",
            "GIT_COMMITTER_EMAIL=${commitEmail}",
            "GIT_SERVER_HOST=${gitHost}",
            "GH_HOST=${gitHost}",
        ]

        stage('Checkout') {
            checkout scm
            sh "chmod +x '${ws}/scripts/'*.sh"
        }

        stage('Prepare') {
            if (!(params.PROMPT ?: '').trim() && provider != 'fake') {
                error 'PROMPT is empty: describe the task (repository URL, findings, constraints).'
            }
            sh "rm -rf '${stateDir}' '${agentWorkspace}' '${ws}/config/fake-script.json.cursor' && mkdir -p '${stateDir}' '${agentWorkspace}'"
            writeFile file: promptFile, text: (params.PROMPT ?: 'Run the scripted smoke flow.'), encoding: 'UTF-8'
            String skillsRepo = folderProp('SKILLS_REPO_URL', '')
            if (skillsRepo) {
                dir("${ws}/external-skills") {
                    withCredentials([usernamePassword(credentialsId: folderProp('GIT_CREDENTIALS_ID', 'ghe-token'),
                                                      usernameVariable: 'GIT_USERNAME', passwordVariable: 'GIT_PASSWORD')]) {
                        withEnv(baseEnv) {
                            sh "rm -rf ./* && git clone --depth 1 --branch '${folderProp('SKILLS_REPO_REF', 'master')}' '${skillsRepo}' ."
                        }
                    }
                }
                skillDirs << "${ws}/external-skills"
            }
            withEnv(baseEnv) {
                sh "'${ws}/scripts/check-node.sh'"
            }
        }

        List commonArgs = [
            "--provider '${provider}'",
            "--model '${params.MODEL}'",
            "--effort '${params.THINKING_LEVEL}'",
            "--context-tokens ${contextTokensFor(params.CONTEXT_SIZE)}",
            "--skill '${params.SKILL ?: 'none'}'",
            "--workspace '${agentWorkspace}'",
            "--state-dir '${stateDir}'",
            "--session-id '${env.BUILD_TAG}'",
            "--max-iterations ${params.MAX_ITERATIONS ?: '200'}",
            "--git-host '${gitHost}'",
        ]
        for (String dirPath : skillDirs) { commonArgs << "--skills-dir '${dirPath}'" }
        if (params.DRY_RUN) { commonArgs << '--dry-run' }
        if (provider == 'fake') { commonArgs << "--fake-script '${ws}/config/fake-script.json'" }
        String common = commonArgs.join(' ')

        Map finalState = [:]
        try {
            stage('Validate') {
                withEnv(baseEnv) {
                    withCredentials(credentialsFor(provider)) {
                        sh "python3 -m pipeline_agent check ${common}"
                    }
                }
            }
            stage('Agent') {
                withEnv(baseEnv) {
                    withCredentials(credentialsFor(provider)) {
                        finalState = runAgentLoop(common, stateDir, promptFile, inputTimeout)
                    }
                }
            }
        } finally {
            stage('Report') {
                archiveArtifacts artifacts: '.agent/**', allowEmptyArchive: true
                Map state = finalState ?: readAgentState(stateDir)
                String reportPath = "${stateDir}/final_message.md"
                if (fileExists(reportPath)) {
                    echo "===== agent report =====\n${readFile(file: reportPath, encoding: 'UTF-8')}\n========================"
                }
                String status = state['STATUS'] ?: 'unknown'
                currentBuild.description = "${params.SKILL} | ${provider}/${params.MODEL} | ${status} | calls ${state['MODEL_CALLS'] ?: '?'} | tokens in ${state['INPUT_TOKENS'] ?: '?'} out ${state['OUTPUT_TOKENS'] ?: '?'}"
            }
        }
    }
}

String label = (params.AGENT_LABEL ?: '').trim()
if (label) {
    node(label) { pipelineBody() }
} else {
    node { pipelineBody() }
}
