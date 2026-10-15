import { pathToFileURL } from "node:url"
const [pluginPath, stepsJson] = process.argv.slice(2)
const { WatchmanLive } = await import(pathToFileURL(pluginPath).href)
const prompts = [], logs = []
const client = {
  app: { log: async (a) => { logs.push(a.body.message) } },
  session: {
    messages: async () => ({ data: [{ info: { role: "user" }, parts: [{ type: "text", text: "Add a loader" }] }] }),
    prompt: async (a) => { prompts.push(a.body.parts[0].text) },
  },
}
const hooks = await WatchmanLive({ client })
const results = []
for (const step of JSON.parse(stepsJson)) {
  const r = { step: step.name, threw: null, output: null }
  try {
    if (step.hook === "before") {
      await hooks["tool.execute.before"]({ tool: step.tool ?? "bash", sessionID: "s1", callID: step.call }, { args: { command: step.command } })
    } else if (step.hook === "after") {
      const out = { output: step.output ?? "ok" }
      await hooks["tool.execute.after"]({ tool: step.tool ?? "bash", sessionID: "s1", callID: step.call }, out)
      r.output = out.output
    } else if (step.hook === "idle") {
      await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
      await new Promise((res) => setTimeout(res, 150))
    }
  } catch (e) { r.threw = String(e.message) }
  r.prompts = prompts.length
  results.push(r)
}
console.log(JSON.stringify({ results, prompts, logs }))
