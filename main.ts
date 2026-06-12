// OKX API Proxy for Deno Deploy
import { serve } from "https://deno.land/std@0.220.0/http/server.ts";

async function handler(request: Request): Promise<Response> {
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
      }
    });
  }

  try {
    const url = new URL(request.url);
    const targetPath = url.searchParams.get("path");
    const targetHost = url.searchParams.get("host") || "www.okx.com";

    if (!targetPath) {
      return new Response(JSON.stringify({
        status: "ok",
        service: "okx-api-proxy",
        usage: "?path=/api/v5/public/time&host=www.okx.com"
      }), {
        headers: {
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "*"
        }
      });
    }

    // 过滤掉 path 和 host 参数，保留其他 query params
    const params = new URLSearchParams(url.searchParams);
    params.delete("path");
    params.delete("host");
    const qs = params.toString() ? "?" + params.toString() : "";
    const targetUrl = `https://${targetHost}${targetPath}${qs}`;

    // 构建转发请求头
    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.delete("content-length");
    headers.delete("connection");
    headers.delete("x-forwarded-for");

    const fetchOptions: RequestInit = {
      method: request.method,
      headers: headers,
    };

    if (request.method === "POST" && request.body) {
      fetchOptions.body = await request.arrayBuffer();
    }

    const resp = await fetch(targetUrl, fetchOptions);

    const respHeaders = new Headers(resp.headers);
    respHeaders.set("Access-Control-Allow-Origin", "*");

    return new Response(resp.body, {
      status: resp.status,
      headers: respHeaders,
    });

  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return new Response(JSON.stringify({ error: msg }), {
      status: 500,
      headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
    });
  }
}

serve(handler);
