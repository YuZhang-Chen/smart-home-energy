import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./style.css";
import "@copilotkit/react-core/v2/styles.css";
import { CopilotKitProvider } from "@copilotkit/react-core/v2";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <CopilotKitProvider runtimeUrl="/api/copilotkit" agentId="home">
      <App />
    </CopilotKitProvider>
  </StrictMode>,
);
