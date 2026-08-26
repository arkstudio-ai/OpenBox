import { describe, expect, it } from "vitest"
import { parseMcpConfig } from "./parse-mcp-config"

// The snippets below are the shapes MCP servers are documented in. Someone
// adding a server pastes what the README gave them, so each of these has to
// land on the same {name, config} without being retyped.
describe("parseMcpConfig", () => {
  it("reads the Claude Desktop mcpServers wrapper", () => {
    const { entries, error } = parseMcpConfig(`{
      "mcpServers": {
        "memory": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-memory"]
        }
      }
    }`)
    expect(error).toBeUndefined()
    expect(entries).toHaveLength(1)
    expect(entries[0].name).toBe("memory")
    expect(entries[0].config.type).toBe("stdio")
    expect(entries[0].config.args).toEqual(["-y", "@modelcontextprotocol/server-memory"])
  })

  it("reads the VS Code servers wrapper", () => {
    const { entries } = parseMcpConfig('{"servers":{"fs":{"command":"npx","args":["-y","x"]}}}')
    expect(entries.map((e) => e.name)).toEqual(["fs"])
  })

  it("reads several servers from one paste", () => {
    const { entries } = parseMcpConfig(`{
      "mcpServers": {
        "a": { "command": "npx", "args": ["-y", "a"] },
        "b": { "url": "https://example.com/mcp" }
      }
    }`)
    expect(entries.map((e) => e.name).sort()).toEqual(["a", "b"])
    expect(entries.find((e) => e.name === "b")?.config.type).toBe("remote")
  })

  it("treats a url as remote even when no type is declared", () => {
    const { entries } = parseMcpConfig('{"mcpServers":{"dw":{"url":"https://mcp.deepwiki.com/mcp"}}}')
    expect(entries[0].config).toMatchObject({
      type: "remote",
      url: "https://mcp.deepwiki.com/mcp",
    })
  })

  it("accepts the transport aliases servers document themselves with", () => {
    for (const declared of ["http", "sse", "streamable-http", "remote"]) {
      const { entries } = parseMcpConfig(
        `{"mcpServers":{"s":{"type":"${declared}","url":"https://e.com/mcp"}}}`,
      )
      expect(entries[0].config.type, declared).toBe("remote")
    }
  })

  it("names a bare single-server object from the fallback", () => {
    const { entries } = parseMcpConfig('{"command":"npx","args":["-y","x"]}', "my-server")
    expect(entries[0].name).toBe("my-server")
  })

  it("prefers a name carried inside the object over the fallback", () => {
    const { entries } = parseMcpConfig('{"name":"inner","command":"npx"}', "outer")
    expect(entries[0].name).toBe("inner")
  })

  it("refuses a bare single-server object with nothing to name it", () => {
    expect(parseMcpConfig('{"command":"npx"}').error).toBe("needName")
  })

  it("carries env and headers through as strings", () => {
    const { entries } = parseMcpConfig(`{
      "mcpServers": {
        "a": { "command": "npx", "env": { "KEY": "v", "PORT": 8080 } },
        "b": { "url": "https://e.com/mcp", "headers": { "Authorization": "Bearer t" } }
      }
    }`)
    expect(entries.find((e) => e.name === "a")?.config.env).toEqual({ KEY: "v", PORT: "8080" })
    expect(entries.find((e) => e.name === "b")?.config.headers).toEqual({
      Authorization: "Bearer t",
    })
  })

  it("reports malformed input rather than throwing", () => {
    expect(parseMcpConfig("not json").error).toBe("invalidJson")
    expect(parseMcpConfig("").error).toBe("empty")
    expect(parseMcpConfig("[1,2]").error).toBe("invalidShape")
  })

  it("skips entries that carry neither a command nor a url", () => {
    const { entries, error } = parseMcpConfig('{"mcpServers":{"broken":{"foo":"bar"}}}')
    expect(entries).toHaveLength(0)
    expect(error).toBe("noServers")
  })
})
